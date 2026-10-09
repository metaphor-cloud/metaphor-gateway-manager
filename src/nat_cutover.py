"""NAT-only cutover Lambda.

The NAT-only gateway is an Auto Scaling group of one. Its private route tables
and its Elastic IP point at ONE persistent, stack-owned ENI, set once at create
and never changed. Replacing the instance means moving that ENI, and this
function does it inside the NEW instance's launch lifecycle, so a failed move
ends as ABANDON while the old instance is still serving:

  1. SourceDestCheck=false on the new instance's primary ENI. The launch
     template cannot set it, and forwarded return traffic can leave by it.
  2. Wait for the instance to tag itself metaphor:gateway-state=ready (its
     /health is 200), sending lifecycle heartbeats meanwhile.
  3. Detach the persistent ENI from whichever instance holds it; wait until it
     is available.
  4. Attach it to the new instance at device 1; wait until it is attached.
  5. Wait for the instance to tag itself metaphor:gateway-state=egress (the gateway
     reports the device-1 interface as its egress).
  6. CONTINUE. The refresh then terminates the old instance.

Any failure after step 3 re-attaches the ENI to the instance it came from, if
that instance is still running, and completes with ABANDON; the ASG terminates
the new instance and the refresh fails with the old one untouched.

The function runs OUTSIDE the VPC on purpose: from a private subnet its only
path to the EC2 and Auto Scaling APIs would be this gateway, through the very
ENI it is moving.

Idempotent on redelivery: it reads where the ENI is before acting, so a retried
event that finds the ENI already on the new instance goes straight to step 5.

One function serves every NAT-only group of a stack. NAT_ASG_NAMES and
EGRESS_ENI_IDS are comma-separated lists paired by position: each group and the
egress ENI it owns. The event's AutoScalingGroupName picks the pair. A launch in
a group with no entry is ABANDONed (reason=unknown_group): the instance it would
replace keeps serving.

A refresh whose replacements cannot complete a cutover (a bad AMI) would
otherwise launch and abandon instances forever: after an ABANDON
during an instance refresh that has already abandoned MAX_REFRESH_ABANDONS - 1
launches, the refresh is cancelled. The count comes from the ASG's own scaling
activities since the refresh started, so there is no state to keep. The
cancellation is published to NOTIFY_TOPIC_ARN (the stack's AMI-update topic)
when the stack has one.
"""

import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STATE_TAG = "metaphor:gateway-state"


def egress_enis(asg_names, eni_ids):
    names = [n.strip() for n in asg_names.split(",") if n.strip()]
    enis = [e.strip() for e in eni_ids.split(",") if e.strip()]
    if len(names) != len(enis):
        raise ValueError(f"NAT_ASG_NAMES has {len(names)} entries but EGRESS_ENI_IDS has {len(enis)}")
    return dict(zip(names, enis))


class Config:
    def __init__(self, env=None, asg=None):
        env = env if env is not None else os.environ
        self.egress_enis = egress_enis(env["NAT_ASG_NAMES"], env["EGRESS_ENI_IDS"])
        # The ENI this invocation moves: the one owned by the launching group.
        self.eni_id = self.egress_enis.get(asg)
        self.device_index = int(env.get("EGRESS_DEVICE_INDEX", "1"))
        self.ready_timeout = int(env.get("READY_TIMEOUT_SECONDS", "480"))
        self.egress_timeout = int(env.get("EGRESS_TIMEOUT_SECONDS", "180"))
        self.poll_seconds = float(env.get("POLL_SECONDS", "10"))
        self.heartbeat_seconds = int(env.get("HEARTBEAT_SECONDS", "60"))
        self.max_refresh_abandons = int(env.get("MAX_REFRESH_ABANDONS", "2"))
        self.notify_topic_arn = env.get("NOTIFY_TOPIC_ARN", "")


class Clients:
    def __init__(self):
        self.ec2 = boto3.client("ec2")
        self.asg = boto3.client("autoscaling")
        self.sns = boto3.client("sns")


class CutoverFailed(Exception):
    """The cutover cannot complete; the reason code is the message."""


def instance(clients, iid):
    r = clients.ec2.describe_instances(InstanceIds=[iid])["Reservations"]
    return r[0]["Instances"][0] if r and r[0]["Instances"] else None


def gateway_state(inst):
    for t in inst.get("Tags", []):
        if t["Key"] == STATE_TAG:
            return t["Value"]
    return None


def primary_eni(inst):
    for n in inst.get("NetworkInterfaces", []):
        if n.get("Attachment", {}).get("DeviceIndex") == 0:
            return n["NetworkInterfaceId"]
    return None


def eni(clients, eni_id):
    return clients.ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"][0]


class Hook:
    """Lifecycle action bookkeeping: heartbeats while waiting, one completion."""

    def __init__(self, cfg, clients, detail, clock=time.monotonic):
        self.cfg, self.clients, self.clock = cfg, clients, clock
        self.name = detail["LifecycleHookName"]
        self.asg = detail["AutoScalingGroupName"]
        self.token = detail["LifecycleActionToken"]
        self.iid = detail["EC2InstanceId"]
        self.last_beat = clock()

    def beat(self):
        if self.clock() - self.last_beat < self.cfg.heartbeat_seconds:
            return
        try:
            self.clients.asg.record_lifecycle_action_heartbeat(
                LifecycleHookName=self.name, AutoScalingGroupName=self.asg,
                LifecycleActionToken=self.token, InstanceId=self.iid)
        except Exception as e:
            logger.warning("lifecycle heartbeat for %s failed: reason=%s: %s", self.iid, type(e).__name__, e)
        self.last_beat = self.clock()

    def complete(self, result):
        self.clients.asg.complete_lifecycle_action(
            LifecycleHookName=self.name, AutoScalingGroupName=self.asg,
            LifecycleActionToken=self.token, InstanceId=self.iid,
            LifecycleActionResult=result)


def wait_for(cfg, hook, what, timeout, check, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        if check():
            return
        if clock() >= deadline:
            raise CutoverFailed(f"{what}_timeout")
        hook.beat()
        sleep(cfg.poll_seconds)


def wait_state(cfg, clients, hook, iid, want, timeout, **kw):
    def check():
        inst = instance(clients, iid)
        if inst is None or inst["State"]["Name"] not in ("pending", "running"):
            raise CutoverFailed("instance_gone")
        return gateway_state(inst) == want
    wait_for(cfg, hook, f"state_{want}", timeout, check, **kw)


def detach(cfg, clients, hook, eni_id, **kw):
    e = eni(clients, eni_id)
    att = e.get("Attachment")
    if not att:
        return None
    holder = att.get("InstanceId")
    logger.info("detaching %s from %s", eni_id, holder)
    clients.ec2.detach_network_interface(AttachmentId=att["AttachmentId"])
    wait_for(cfg, hook, "detach", 120, lambda: eni(clients, eni_id)["Status"] == "available", **kw)
    return holder


def attach(cfg, clients, hook, eni_id, iid, **kw):
    logger.info("attaching %s to %s at device %d", eni_id, iid, cfg.device_index)
    clients.ec2.attach_network_interface(
        NetworkInterfaceId=eni_id, InstanceId=iid, DeviceIndex=cfg.device_index)
    wait_for(cfg, hook, "attach", 120,
             lambda: eni(clients, eni_id).get("Attachment", {}).get("Status") == "attached", **kw)


def cutover(cfg, clients, hook, iid, **kw):
    """Steps 1-5. Returns normally on success; raises CutoverFailed otherwise.
    Restores the ENI to its previous holder on a failure after it moved."""
    inst = instance(clients, iid)
    if inst is None:
        raise CutoverFailed("instance_gone")
    p = primary_eni(inst)
    if p:
        clients.ec2.modify_network_interface_attribute(NetworkInterfaceId=p, SourceDestCheck={"Value": False})
    else:
        raise CutoverFailed("no_primary_eni")

    current = eni(clients, cfg.eni_id).get("Attachment", {}).get("InstanceId")
    previous = None
    if current != iid:
        wait_state(cfg, clients, hook, iid, "ready", cfg.ready_timeout, **kw)
        try:
            previous = detach(cfg, clients, hook, cfg.eni_id, **kw)
            attach(cfg, clients, hook, cfg.eni_id, iid, **kw)
        except Exception:
            restore(cfg, clients, hook, previous, **kw)
            raise
    else:
        logger.info("%s already holds %s (redelivered event); confirming egress", iid, cfg.eni_id)

    try:
        wait_state(cfg, clients, hook, iid, "egress", cfg.egress_timeout, **kw)
    except Exception:
        restore(cfg, clients, hook, previous, **kw)
        raise


def restore(cfg, clients, hook, previous, **kw):
    """Put the ENI back on the instance it came from, if there was one and it
    is still running. Best effort: a failure here is logged at error, because
    it leaves the stack's egress unattached until the next launch."""
    if not previous:
        return
    try:
        inst = instance(clients, previous)
        if inst is None or inst["State"]["Name"] != "running":
            logger.error("cannot restore %s: previous holder %s is not running (reason=restore_target_gone)",
                         cfg.eni_id, previous)
            return
        detach(cfg, clients, hook, cfg.eni_id, **kw)
        attach(cfg, clients, hook, cfg.eni_id, previous, **kw)
        logger.info("restored %s to %s", cfg.eni_id, previous)
    except Exception as e:
        logger.error("restoring %s to %s failed (reason=restore_failed): %s: %s",
                     cfg.eni_id, previous, type(e).__name__, e)


def abandoned_launches_since(clients, asg, since):
    """Launch activities the ASG cancelled because a lifecycle action was
    abandoned, from `since` on."""
    n = 0
    for page in clients.asg.get_paginator("describe_scaling_activities").paginate(AutoScalingGroupName=asg):
        for a in page["Activities"]:
            if a["StartTime"] < since:
                return n
            if (a.get("StatusCode") == "Cancelled" and a.get("Description", "").startswith("Launching")
                    and "abandoned" in a.get("StatusMessage", "")):
                n += 1
    return n


def stop_failing_refresh(cfg, clients, asg):
    """Cancel the in-progress instance refresh once its launches keep failing
    their cutover. Called after completing a hook with ABANDON; the activity
    for that abandon may not be recorded yet, so it is counted as one more."""
    try:
        r = clients.asg.describe_instance_refreshes(AutoScalingGroupName=asg, MaxRecords=1)
        refreshes = [x for x in r.get("InstanceRefreshes", []) if x["Status"] in ("Pending", "InProgress")]
        if not refreshes:
            return False
        refresh = refreshes[0]
        earlier = abandoned_launches_since(clients, asg, refresh["StartTime"])
        if earlier + 1 < cfg.max_refresh_abandons:
            logger.warning("instance refresh %s: %d abandoned cutover(s) so far, allowing another attempt",
                           refresh["InstanceRefreshId"], earlier + 1)
            return False
        clients.asg.cancel_instance_refresh(AutoScalingGroupName=asg)
        logger.error("cancelled instance refresh %s after %d abandoned cutovers (reason=refresh_cancelled); "
                     "the current gateway keeps the egress interface", refresh["InstanceRefreshId"], earlier + 1)
        notify_refresh_cancelled(cfg, clients, asg, refresh["InstanceRefreshId"], earlier + 1)
        return True
    except Exception as e:
        logger.error("could not evaluate or cancel the instance refresh for %s (reason=refresh_check_failed): %s: %s",
                     asg, type(e).__name__, e)
        return False


def notify_refresh_cancelled(cfg, clients, asg, refresh_id, abandons):
    """Tell the operator; best-effort, the cancellation already happened."""
    if not cfg.notify_topic_arn:
        logger.info("no notification topic configured; refresh %s cancellation not published", refresh_id)
        return
    try:
        clients.sns.publish(
            TopicArn=cfg.notify_topic_arn,
            Subject=f"Metaphor Gateway: update cancelled on {asg}"[:100],
            Message=(f"Instance refresh {refresh_id} on {asg} was cancelled after {abandons} replacement "
                     f"gateways failed their cutover (reason=refresh_cancelled). The current gateway is "
                     f"still serving and keeps the egress interface. The new image did not report ready or "
                     f"did not take over egress; see /aws/lambda/{os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'the cutover function')} "
                     f"for the per-attempt reason."),
        )
    except Exception as e:
        logger.error("could not publish the refresh cancellation for %s (reason=notify_failed): %s: %s",
                     asg, type(e).__name__, e)


def handler(event, context):
    d = event["detail"]
    if "LAUNCHING" not in d.get("LifecycleTransition", ""):
        logger.info("ignoring %s for %s", d.get("LifecycleTransition"), d.get("EC2InstanceId"))
        return {"status": "ignored"}
    cfg, clients = Config(asg=d["AutoScalingGroupName"]), Clients()
    hook = Hook(cfg, clients, d)
    iid = d["EC2InstanceId"]
    result = "ABANDON"
    try:
        if cfg.eni_id is None:
            raise CutoverFailed("unknown_group")
        cutover(cfg, clients, hook, iid)
        result = "CONTINUE"
        logger.info("cutover to %s complete: %s at device %d", iid, cfg.eni_id, cfg.device_index)
    except CutoverFailed as e:
        logger.error("cutover to %s failed, completing with ABANDON: reason=%s", iid, e)
    except Exception as e:
        logger.error("cutover to %s failed, completing with ABANDON: reason=%s: %s", iid, type(e).__name__, e)
    finally:
        hook.complete(result)
    if result == "ABANDON":
        stop_failing_refresh(cfg, clients, d["AutoScalingGroupName"])
    return {"status": result}

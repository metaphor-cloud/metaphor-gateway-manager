"""Rolls Network by Metaphor Auto Scaling groups onto a newer 'latest' or 'stable' AMI.

Runs on a schedule. For each group in ASG_NAMES it compares the AMIs the
group's instances run with the latest published Marketplace AMI (SSM_PARAM) and,
only when they differ and no refresh is already running, starts an instance
refresh with REFRESH_PREFERENCES (JSON). New instances always resolve 'latest'
at launch; this only handles instances that are already running.

ASG_NAMES is a list so one function serves a gwlb fleet (one group) and
NAT-only gateways (one group of one per egress interface). In NAT-only the
preferences are MinHealthyPercentage 100 with MaxHealthyPercentage 200: the
replacement launches first and the cutover function moves the egress interface
onto it, so connections open through the old gateway reset at the switch.

A gwlb fleet with an Elastic IP pool (EIPS_PER_AZ >= 2) can only launch first
while every AZ has a free pool slot: the EIP manager makes a new gateway wait
for a slot rather than egress from a transient. When some AZ already runs as
many gateways as it has slots, the refresh is turned terminate-first, one
gateway at a time, so the outgoing gateway's slot frees for its replacement.

'stable' is the newest release that was 'latest' for STABLE_MIN_AGE_DAYS (2 by
default) without a newer one replacing it, so production only runs a release
that test and dev ran as their newest for that long. A release superseded
sooner never becomes stable.
Marketplace publishes only a parameter per version and 'latest', so with
STABLE_PARAM set this lists the versions under PRODUCT_PATH, dates each by the
parameter's LastModifiedDate (when Marketplace published it), and writes the
chosen AMI to STABLE_PARAM, a parameter the stack owns and its launch template
resolves at each launch. Then
it refreshes groups behind that AMI, as for 'latest'. The same function serves
a Custom::StableAmi resource, which writes STABLE_PARAM on Create and Update so
the first gateway has an AMI to resolve, and removes it on Delete.
"""

import json
import logging
import os
import re
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class Config:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.asg_names = [n.strip() for n in env["ASG_NAMES"].split(",") if n.strip()]
        self.ssm_param = env.get("SSM_PARAM", "")
        self.stable_param = env.get("STABLE_PARAM", "")
        self.product_path = env.get("PRODUCT_PATH", "").rstrip("/")
        self.stable_min_age = timedelta(days=float(env.get("STABLE_MIN_AGE_DAYS", "2") or "2"))
        if self.stable_param and not self.product_path:
            raise ValueError("STABLE_PARAM needs PRODUCT_PATH")
        if not (self.ssm_param or self.stable_param):
            raise ValueError("set SSM_PARAM or STABLE_PARAM")
        self.topic_arn = env.get("TOPIC_ARN", "")
        self.preferences = json.loads(env["REFRESH_PREFERENCES"])
        self.eips_per_az = int(env.get("EIPS_PER_AZ", "0") or "0")


class Clients:
    def __init__(self):
        self.ssm = boto3.client("ssm")
        self.asg = boto3.client("autoscaling")
        self.ec2 = boto3.client("ec2")
        self.sns = boto3.client("sns")


VERSION = re.compile(r"^\d+(\.\d+)*$")


def version_key(name):
    """(1, 0, 10) for '.../1.0.10'; None for 'latest' or any other non-version name."""
    leaf = name.rsplit("/", 1)[-1]
    return tuple(int(p) for p in leaf.split(".")) if VERSION.match(leaf) else None


def stable_release(clients, path, min_age, now=None):
    """(version, AMI) of the newest version that was the latest for min_age.

    A version's time as latest runs from its publication to the next newer
    version's, or to now for the newest. Versions sort numerically, so 1.0.10 is
    newer than 1.0.9. When no version qualifies (a product's first week), the
    oldest published version is stable.
    """
    now = now or datetime.now(timezone.utc)
    found = []
    for page in clients.ssm.get_paginator("get_parameters_by_path").paginate(Path=path):
        for p in page["Parameters"]:
            key = version_key(p["Name"])
            if key is not None:
                found.append((key, p["Name"].rsplit("/", 1)[-1], p["Value"], p["LastModifiedDate"]))
    if not found:
        raise RuntimeError(f"no published versions under {path}")
    found.sort()
    chosen = None
    for i, (_, version, ami, published) in enumerate(found):
        until = found[i + 1][3] if i + 1 < len(found) else now
        if until - published >= min_age:
            chosen = (version, ami, until - published)
    if chosen is None:
        _, version, ami, _ = found[0]
        logger.warning("no version under %s was latest for %s; stable falls back to the oldest, %s "
                       "(reason=nothing_soaked)", path, min_age, version)
        return version, ami
    version, ami, held = chosen
    logger.info("stable is %s (%s, latest for %s); newest of %d published is %s",
                version, ami, held, len(found), found[-1][1])
    return version, ami


def publish_stable(cfg, clients):
    """Write the stable AMI to STABLE_PARAM when it moved, and return it."""
    version, ami = stable_release(clients, cfg.product_path, cfg.stable_min_age)
    try:
        current = clients.ssm.get_parameter(Name=cfg.stable_param)["Parameter"]["Value"]
    except clients.ssm.exceptions.ParameterNotFound:
        current = None
    if current != ami:
        # aws:ec2:image is the data type EC2 expects behind a resolve:ssm ImageId.
        clients.ssm.put_parameter(Name=cfg.stable_param, Value=ami, Type="String",
                                  DataType="aws:ec2:image", Overwrite=True)
        logger.info("%s moved from %s to %s (stable %s)", cfg.stable_param, current or "nothing", ami, version)
    return ami


def cfn_send(event, context, status, data, reason=""):
    body = json.dumps({
        "Status": status,
        "Reason": reason or "See " + getattr(context, "log_stream_name", "the function log"),
        "PhysicalResourceId": event.get("PhysicalResourceId") or event["ResourceProperties"].get("ParameterName", "stable-ami"),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, headers={"content-type": ""}, method="PUT")
    urllib.request.urlopen(req)


def stable_resource(event, context, cfg, clients):
    """Custom::StableAmi: STABLE_PARAM exists while the resource does."""
    kind = event["RequestType"]
    try:
        if not cfg.stable_param:
            raise ValueError("this function has no STABLE_PARAM")
        if kind == "Delete":
            try:
                clients.ssm.delete_parameter(Name=cfg.stable_param)
            except clients.ssm.exceptions.ParameterNotFound:
                pass
            cfn_send(event, context, "SUCCESS", {})
            return
        ami = publish_stable(cfg, clients)
        cfn_send(event, context, "SUCCESS", {"AmiId": ami, "ParameterName": cfg.stable_param})
    except Exception as e:
        logger.error("stable AMI %s failed (reason=stable_resource_failed): %s: %s", kind, type(e).__name__, e)
        cfn_send(event, context, "FAILED", {}, reason=f"{type(e).__name__}: {e}"[:400])


def running_amis(clients, group):
    ids = [i["InstanceId"] for i in group.get("Instances", [])]
    if not ids:
        return set()
    amis = set()
    for page in clients.ec2.get_paginator("describe_instances").paginate(InstanceIds=ids):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                amis.add(inst["ImageId"])
    return amis


def refresh_in_progress(clients, name):
    r = clients.asg.describe_instance_refreshes(AutoScalingGroupName=name, MaxRecords=1)
    refreshes = r.get("InstanceRefreshes", [])
    return bool(refreshes) and refreshes[0]["Status"] in ("Pending", "InProgress", "Cancelling")


def refresh_preferences(cfg, group):
    """REFRESH_PREFERENCES, made terminate-first when an EIP pool is full."""
    prefs = dict(cfg.preferences)
    if cfg.eips_per_az < 2:
        return prefs
    per_az = Counter(i["AvailabilityZone"] for i in group.get("Instances", []))
    fullest = max(per_az.values(), default=0)
    if fullest < cfg.eips_per_az:
        return prefs
    desired = max(int(group.get("DesiredCapacity", 1)), 1)
    # Keep desired-1 healthy so exactly one gateway is out at a time. Rounded up
    # (100 - floor(100/N)): whichever way Auto Scaling rounds the percentage to
    # instances, it never takes out two, and it always replaces at least one.
    # Max 100 is what makes it terminate before it launches.
    prefs["MinHealthyPercentage"] = min(100 - 100 // desired, 99)
    prefs["MaxHealthyPercentage"] = 100
    logger.info("%s has %d gateway(s) in an AZ with %d pool EIP(s); refreshing terminate-first "
                "one at a time (reason=pool_full)", group.get("AutoScalingGroupName", "group"),
                fullest, cfg.eips_per_az)
    return prefs


def notify(cfg, clients, name, target, refresh_id):
    if not cfg.topic_arn:
        return
    try:
        clients.sns.publish(
            TopicArn=cfg.topic_arn,
            Subject=f"Network by Metaphor: rolling {name} to a new AMI"[:100],
            Message=(
                f"A newer Network by Metaphor AMI was detected.\n\n"
                f"  Group:       {name}\n"
                f"  New AMI:     {target}\n"
                f"  Refresh ID:  {refresh_id}\n\n"
                f"An Auto Scaling instance refresh has been started to roll the group.\n"
            ),
        )
    except Exception as e:
        logger.warning("sns publish for %s failed (reason=notify_failed): %s: %s", name, type(e).__name__, e)


def update_group(cfg, clients, name, target):
    groups = clients.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[name])["AutoScalingGroups"]
    if not groups:
        logger.warning("auto scaling group %s not found (reason=no_asg)", name)
        return {"status": "no_asg"}
    current = running_amis(clients, groups[0])
    logger.info("%s runs %s; target is %s", name, sorted(current) or "nothing", target)
    if current and current == {target}:
        return {"status": "up_to_date", "ami_id": target}
    if refresh_in_progress(clients, name):
        logger.info("%s already has an instance refresh in progress; skipping (reason=refresh_in_progress)", name)
        return {"status": "refresh_in_progress"}
    refresh_id = clients.asg.start_instance_refresh(
        AutoScalingGroupName=name, Preferences=refresh_preferences(cfg, groups[0]))["InstanceRefreshId"]
    logger.info("started instance refresh %s to roll %s to %s", refresh_id, name, target)
    notify(cfg, clients, name, target, refresh_id)
    return {"status": "refresh_started", "ami_id": target, "refresh_id": refresh_id}


def handler(event, context, cfg=None, clients=None):
    cfg = cfg or Config()
    clients = clients or Clients()
    if isinstance(event, dict) and "RequestType" in event:
        return stable_resource(event, context, cfg, clients)
    if cfg.stable_param:
        target = publish_stable(cfg, clients)
    else:
        target = clients.ssm.get_parameter(Name=cfg.ssm_param)["Parameter"]["Value"]
    results = {}
    failed = []
    # One group failing must not stop the others from rolling.
    for name in cfg.asg_names:
        try:
            results[name] = update_group(cfg, clients, name, target)
        except Exception as e:
            logger.error("updating %s failed (reason=update_failed): %s: %s", name, type(e).__name__, e)
            results[name] = {"status": "error"}
            failed.append(name)
    if failed:
        raise RuntimeError(f"AMI update failed for {', '.join(failed)}")
    return results

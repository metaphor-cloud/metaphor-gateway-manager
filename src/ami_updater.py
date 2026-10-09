"""Rolls Metaphor Gateway Auto Scaling groups onto a newer 'latest' AMI.

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
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class Config:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.asg_names = [n.strip() for n in env["ASG_NAMES"].split(",") if n.strip()]
        self.ssm_param = env["SSM_PARAM"]
        self.topic_arn = env.get("TOPIC_ARN", "")
        self.preferences = json.loads(env["REFRESH_PREFERENCES"])


class Clients:
    def __init__(self):
        self.ssm = boto3.client("ssm")
        self.asg = boto3.client("autoscaling")
        self.ec2 = boto3.client("ec2")
        self.sns = boto3.client("sns")


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


def notify(cfg, clients, name, latest, refresh_id):
    if not cfg.topic_arn:
        return
    try:
        clients.sns.publish(
            TopicArn=cfg.topic_arn,
            Subject=f"Metaphor Gateway: rolling {name} to a new AMI"[:100],
            Message=(
                f"A newer Metaphor Gateway AMI was detected.\n\n"
                f"  Group:       {name}\n"
                f"  New AMI:     {latest}\n"
                f"  Refresh ID:  {refresh_id}\n\n"
                f"An Auto Scaling instance refresh has been started to roll the group.\n"
            ),
        )
    except Exception as e:
        logger.warning("sns publish for %s failed (reason=notify_failed): %s: %s", name, type(e).__name__, e)


def update_group(cfg, clients, name, latest):
    groups = clients.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[name])["AutoScalingGroups"]
    if not groups:
        logger.warning("auto scaling group %s not found (reason=no_asg)", name)
        return {"status": "no_asg"}
    current = running_amis(clients, groups[0])
    logger.info("%s runs %s; latest is %s", name, sorted(current) or "nothing", latest)
    if current and current == {latest}:
        return {"status": "up_to_date", "ami_id": latest}
    if refresh_in_progress(clients, name):
        logger.info("%s already has an instance refresh in progress; skipping (reason=refresh_in_progress)", name)
        return {"status": "refresh_in_progress"}
    refresh_id = clients.asg.start_instance_refresh(
        AutoScalingGroupName=name, Preferences=cfg.preferences)["InstanceRefreshId"]
    logger.info("started instance refresh %s to roll %s to %s", refresh_id, name, latest)
    notify(cfg, clients, name, latest, refresh_id)
    return {"status": "refresh_started", "ami_id": latest, "refresh_id": refresh_id}


def handler(event, context, cfg=None, clients=None):
    cfg = cfg or Config()
    clients = clients or Clients()
    latest = clients.ssm.get_parameter(Name=cfg.ssm_param)["Parameter"]["Value"]
    results = {}
    failed = []
    # One group failing must not stop the others from rolling.
    for name in cfg.asg_names:
        try:
            results[name] = update_group(cfg, clients, name, latest)
        except Exception as e:
            logger.error("updating %s failed (reason=update_failed): %s: %s", name, type(e).__name__, e)
            results[name] = {"status": "error"}
            failed.append(name)
    if failed:
        raise RuntimeError(f"AMI update failed for {', '.join(failed)}")
    return results

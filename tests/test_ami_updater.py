"""Behaviour tests for the AMI updater.

EC2 and SSM run against moto. moto has no instance refresh API, so the Auto
Scaling client is a small fake that records refreshes it is asked to start.
"""

import json
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

import ami_updater as app

# moto refuses writes under /aws/, so the test uses its own name for the parameter.
PARAM = "/test/marketplace/latest"
PREFS = {"MinHealthyPercentage": 100, "MaxHealthyPercentage": 200, "InstanceWarmup": 60}


class FakeAsg:
    def __init__(self, groups, refresh_status=None, fail=()):
        self.groups = groups
        self.refresh_status = refresh_status or {}
        self.fail = set(fail)
        self.started = []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        name = AutoScalingGroupNames[0]
        if name in self.fail:
            raise RuntimeError("AccessDenied")
        if name not in self.groups:
            return {"AutoScalingGroups": []}
        return {"AutoScalingGroups": [{"Instances": [{"InstanceId": i} for i in self.groups[name]]}]}

    def describe_instance_refreshes(self, AutoScalingGroupName, MaxRecords):
        status = self.refresh_status.get(AutoScalingGroupName)
        return {"InstanceRefreshes": [{"Status": status}] if status else []}

    def start_instance_refresh(self, AutoScalingGroupName, Preferences):
        self.started.append((AutoScalingGroupName, Preferences))
        return {"InstanceRefreshId": f"refresh-{AutoScalingGroupName}"}


@pytest.fixture
def env():
    with mock_aws():
        ec2 = boto3.client("ec2")
        ssm = boto3.client("ssm")
        images = [i["ImageId"] for i in ec2.describe_images(Owners=["amazon"])["Images"][:2]]
        old, new = images
        ssm.put_parameter(Name=PARAM, Value=new, Type="String")

        def launch(ami):
            return ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]

        yield SimpleNamespace(ec2=ec2, ssm=ssm, sns=boto3.client("sns"), old=old, new=new, launch=launch)


def cfg(names, topic=""):
    return app.Config(env={
        "ASG_NAMES": names,
        "SSM_PARAM": PARAM,
        "TOPIC_ARN": topic,
        "REFRESH_PREFERENCES": json.dumps(PREFS),
    })


def clients(env, asg):
    return SimpleNamespace(ssm=env.ssm, ec2=env.ec2, sns=env.sns, asg=asg)


def test_refreshes_only_the_groups_behind_latest(env):
    asg = FakeAsg({"nat-a": [env.launch(env.old)], "nat-b": [env.launch(env.new)]})
    out = app.handler({}, None, cfg("nat-a,nat-b"), clients(env, asg))
    assert asg.started == [("nat-a", PREFS)]
    assert out["nat-a"]["status"] == "refresh_started"
    assert out["nat-b"]["status"] == "up_to_date"


def test_mixed_fleet_is_refreshed(env):
    asg = FakeAsg({"gwlb": [env.launch(env.new), env.launch(env.old)]})
    app.handler({}, None, cfg("gwlb"), clients(env, asg))
    assert [name for name, _ in asg.started] == ["gwlb"]


def test_skips_a_group_with_a_refresh_in_flight(env):
    asg = FakeAsg({"nat-a": [env.launch(env.old)]}, refresh_status={"nat-a": "InProgress"})
    out = app.handler({}, None, cfg("nat-a"), clients(env, asg))
    assert asg.started == []
    assert out["nat-a"]["status"] == "refresh_in_progress"


def test_a_finished_refresh_does_not_block_the_next(env):
    asg = FakeAsg({"nat-a": [env.launch(env.old)]}, refresh_status={"nat-a": "Successful"})
    app.handler({}, None, cfg("nat-a"), clients(env, asg))
    assert [name for name, _ in asg.started] == ["nat-a"]


def test_missing_group_is_reported_not_raised(env):
    asg = FakeAsg({})
    out = app.handler({}, None, cfg("gone"), clients(env, asg))
    assert out["gone"]["status"] == "no_asg"


def test_one_failing_group_still_rolls_the_others_then_raises(env):
    asg = FakeAsg({"nat-b": [env.launch(env.old)]}, fail={"nat-a"})
    with pytest.raises(RuntimeError, match="nat-a"):
        app.handler({}, None, cfg("nat-a,nat-b"), clients(env, asg))
    assert [name for name, _ in asg.started] == ["nat-b"]


def test_publishes_the_refresh_to_the_topic(env):
    sqs = boto3.client("sqs")
    queue = sqs.create_queue(QueueName="q")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    topic = env.sns.create_topic(Name="updates")["TopicArn"]
    env.sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn)

    asg = FakeAsg({"nat-a": [env.launch(env.old)]})
    app.handler({}, None, cfg("nat-a", topic=topic), clients(env, asg))

    body = json.loads(sqs.receive_message(QueueUrl=queue)["Messages"][0]["Body"])
    assert "refresh-nat-a" in body["Message"]
    assert env.new in body["Message"]

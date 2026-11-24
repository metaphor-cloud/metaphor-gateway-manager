"""Behaviour tests for the AMI updater.

EC2 and SSM run against moto. moto has no instance refresh API, so the Auto
Scaling client is a small fake that records refreshes it is asked to start.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

import ami_updater as app

# moto refuses writes under /aws/, so the test uses its own name for the parameter.
PARAM = "/test/marketplace/latest"
PREFS = {"MinHealthyPercentage": 100, "MaxHealthyPercentage": 200, "InstanceWarmup": 60}


class FakeAsg:
    def __init__(self, groups, refresh_status=None, fail=(), azs=None):
        self.groups = groups
        self.azs = azs or {}
        self.refresh_status = refresh_status or {}
        self.fail = set(fail)
        self.started = []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        name = AutoScalingGroupNames[0]
        if name in self.fail:
            raise RuntimeError("AccessDenied")
        if name not in self.groups:
            return {"AutoScalingGroups": []}
        ids = self.groups[name]
        azs = self.azs.get(name, ["us-east-1a"] * len(ids))
        return {"AutoScalingGroups": [{
            "AutoScalingGroupName": name,
            "DesiredCapacity": len(ids),
            "Instances": [{"InstanceId": i, "AvailabilityZone": az} for i, az in zip(ids, azs)],
        }]}

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


def cfg(names, topic="", eips_per_az=None, prefs=PREFS):
    env = {
        "ASG_NAMES": names,
        "SSM_PARAM": PARAM,
        "TOPIC_ARN": topic,
        "REFRESH_PREFERENCES": json.dumps(prefs),
    }
    if eips_per_az is not None:
        env["EIPS_PER_AZ"] = str(eips_per_az)
    return app.Config(env=env)


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


GWLB_PREFS = {"MinHealthyPercentage": 100, "InstanceWarmup": 120}


def test_full_eip_pool_refreshes_terminate_first_one_at_a_time(env):
    # Two gateways in one AZ holding both pool EIPs: launching first would leave
    # the replacement with no slot, so the outgoing gateway must go first.
    asg = FakeAsg({"gwlb": [env.launch(env.old), env.launch(env.old)]})
    app.handler({}, None, cfg("gwlb", eips_per_az=2, prefs=GWLB_PREFS), clients(env, asg))
    assert asg.started == [("gwlb", {"MinHealthyPercentage": 50, "MaxHealthyPercentage": 100,
                                     "InstanceWarmup": 120})]


def test_eip_pool_with_headroom_keeps_launch_first(env):
    asg = FakeAsg({"gwlb": [env.launch(env.old), env.launch(env.old)]},
                  azs={"gwlb": ["us-east-1a", "us-east-1b"]})
    app.handler({}, None, cfg("gwlb", eips_per_az=2, prefs=GWLB_PREFS), clients(env, asg))
    assert asg.started == [("gwlb", GWLB_PREFS)]


@pytest.mark.parametrize("desired,min_healthy", [(2, 50), (3, 67), (4, 75), (10, 90)])
def test_terminate_first_keeps_all_but_one_gateway(env, desired, min_healthy):
    asg = FakeAsg({"gwlb": [env.launch(env.old) for _ in range(desired)]})
    app.handler({}, None, cfg("gwlb", eips_per_az=2, prefs=GWLB_PREFS), clients(env, asg))
    assert asg.started[0][1]["MinHealthyPercentage"] == min_healthy
    assert asg.started[0][1]["MaxHealthyPercentage"] == 100


@pytest.mark.parametrize("eips_per_az", [None, 0, 1])
def test_without_an_eip_pool_preferences_are_unchanged(env, eips_per_az):
    asg = FakeAsg({"gwlb": [env.launch(env.old), env.launch(env.old)]})
    app.handler({}, None, cfg("gwlb", eips_per_az=eips_per_az, prefs=GWLB_PREFS), clients(env, asg))
    assert asg.started == [("gwlb", GWLB_PREFS)]


# --- stable: the newest version that was latest for two days ------------------

PRODUCT = "/test/marketplace/product"
STABLE = "/flow/stack/stable-ami"
NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


class VersionedSsm:
    """moto's SSM, with get_parameters_by_path answering from a fixed catalogue.

    moto stamps every parameter with the time it was written, and stable is
    decided by how long ago Marketplace published each version, so the
    catalogue carries its own dates.
    """

    def __init__(self, real, catalogue):
        self.real = real
        self.catalogue = catalogue  # [(leaf name, ami, days ago)]
        self.exceptions = real.exceptions

    def __getattr__(self, name):
        return getattr(self.real, name)

    def get_paginator(self, op):
        assert op == "get_parameters_by_path"
        catalogue = self.catalogue

        class Pages:
            def paginate(self, Path):
                params = [{"Name": f"{Path}/{leaf}", "Value": ami, "LastModifiedDate": NOW - timedelta(days=age)}
                          for leaf, ami, age in catalogue]
                # Two pages, so the paginator is actually walked.
                yield {"Parameters": params[:2]}
                yield {"Parameters": params[2:]}
        return Pages()


def stable_cfg(names="nat-a", min_age_days=None):
    env = {"ASG_NAMES": names, "STABLE_PARAM": STABLE, "PRODUCT_PATH": PRODUCT + "/",
           "REFRESH_PREFERENCES": json.dumps(PREFS)}
    if min_age_days is not None:
        env["STABLE_MIN_AGE_DAYS"] = str(min_age_days)
    return app.Config(env=env)


def stable_clients(env, asg, catalogue):
    return SimpleNamespace(ssm=VersionedSsm(env.ssm, catalogue), ec2=env.ec2, sns=env.sns, asg=asg)


@pytest.fixture
def frozen(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(app, "datetime", Frozen)


TWO_DAYS = timedelta(days=2)


def pick(env, catalogue):
    return app.stable_release(SimpleNamespace(ssm=VersionedSsm(env.ssm, catalogue)), PRODUCT, TWO_DAYS, NOW)


def test_stable_is_the_newest_version_that_was_latest_for_two_days(env):
    # 1.0.9 was latest for 7 days; 1.0.10 has been latest for 1.
    assert pick(env, [("1.0.9", "ami-9", 8), ("1.0.10", "ami-10", 1), ("latest", "ami-10", 1),
                      ("1.0.8", "ami-8", 14)]) == ("1.0.9", "ami-9")


def test_age_alone_is_not_enough(env):
    # 1.0.7 is three days old but was replaced half a day after it shipped, and
    # 1.0.8 a day and a half after: neither was ever latest for two days.
    assert pick(env, [("1.0.6", "ami-6", 20), ("1.0.7", "ami-7", 3), ("1.0.8", "ami-8", 2.5),
                      ("1.0.9", "ami-9", 1)]) == ("1.0.6", "ami-6")


def test_the_newest_counts_its_time_up_to_now(env):
    assert pick(env, [("1.0.8", "ami-8", 20), ("1.0.9", "ami-9", 2)]) == ("1.0.9", "ami-9")


def test_versions_sort_as_numbers_not_strings(env):
    # As strings 1.0.9 sorts last and would be judged against nothing newer.
    assert pick(env, [("1.0.9", "ami-9", 30), ("1.0.10", "ami-10", 1), ("1.0.2", "ami-2", 40)]) == ("1.0.9", "ami-9")


def test_nothing_qualifying_falls_back_to_the_oldest(env):
    assert pick(env, [("1.0.1", "ami-1", 1), ("1.0.0", "ami-0", 1.5)]) == ("1.0.0", "ami-0")


def test_no_versions_is_an_error(env):
    with pytest.raises(RuntimeError):
        pick(env, [("latest", "ami-1", 1)])


def test_scheduled_run_writes_stable_and_rolls_groups_onto_it(env, frozen):
    newer_than_stable = env.launch(env.new)
    on_stable = env.launch(env.old)
    asg = FakeAsg({"nat-a": [newer_than_stable], "nat-b": [on_stable]})
    catalogue = [("1.0.8", env.old, 9), ("1.0.9", env.new, 1)]
    out = app.handler({}, None, stable_cfg("nat-a,nat-b"), stable_clients(env, asg, catalogue))
    p = env.ssm.get_parameter(Name=STABLE)["Parameter"]
    assert p["Value"] == env.old and p["DataType"] == "aws:ec2:image"
    # A group ahead of stable (a stack switched from latest) is rolled back onto it.
    assert out == {"nat-a": {"status": "refresh_started", "ami_id": env.old, "refresh_id": "refresh-nat-a"},
                   "nat-b": {"status": "up_to_date", "ami_id": env.old}}


def test_stable_parameter_is_written_only_when_it_moves(env, frozen):
    asg = FakeAsg({"nat-a": [env.launch(env.old)]})
    catalogue = [("1.0.8", env.old, 9), ("1.0.9", env.new, 1)]
    app.handler({}, None, stable_cfg(), stable_clients(env, asg, catalogue))
    app.handler({}, None, stable_cfg(), stable_clients(env, asg, catalogue))
    assert env.ssm.get_parameter(Name=STABLE)["Parameter"]["Version"] == 1
    # Later, 1.0.9 has been latest for 8 days: the parameter moves and the group rolls.
    later = [("1.0.8", env.old, 16), ("1.0.9", env.new, 8)]
    out = app.handler({}, None, stable_cfg(), stable_clients(env, asg, later))
    assert env.ssm.get_parameter(Name=STABLE)["Parameter"]["Value"] == env.new
    assert out["nat-a"]["status"] == "refresh_started"


def test_min_age_is_configurable(env, frozen):
    asg = FakeAsg({"nat-a": [env.launch(env.new)]})
    catalogue = [("1.0.8", env.old, 9), ("1.0.9", env.new, 1)]
    out = app.handler({}, None, stable_cfg(min_age_days=1), stable_clients(env, asg, catalogue))
    assert out["nat-a"]["status"] == "up_to_date"


class Sent:
    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(app, "cfn_send", lambda event, context, status, data, reason="":
                            self.calls.append((event["RequestType"], status, data)))


def cfn_event(kind):
    return {"RequestType": kind, "ResponseURL": "https://example.invalid", "StackId": "s", "RequestId": "r",
            "LogicalResourceId": "StableAmi", "ResourceProperties": {}}


def test_custom_resource_writes_stable_on_create_and_removes_it_on_delete(env, frozen, monkeypatch):
    sent = Sent(monkeypatch)
    asg = FakeAsg({})
    c = stable_clients(env, asg, [("1.0.8", env.old, 9), ("1.0.9", env.new, 1)])
    app.handler(cfn_event("Create"), None, stable_cfg(), c)
    assert env.ssm.get_parameter(Name=STABLE)["Parameter"]["Value"] == env.old
    app.handler(cfn_event("Delete"), None, stable_cfg(), c)
    with pytest.raises(env.ssm.exceptions.ParameterNotFound):
        env.ssm.get_parameter(Name=STABLE)
    # Deleting what is already gone still succeeds, or the stack could never delete.
    app.handler(cfn_event("Delete"), None, stable_cfg(), c)
    assert sent.calls == [("Create", "SUCCESS", {"AmiId": env.old, "ParameterName": STABLE}),
                          ("Delete", "SUCCESS", {}), ("Delete", "SUCCESS", {})]
    assert asg.started == []


def test_custom_resource_reports_failure_rather_than_hanging(env, frozen, monkeypatch):
    sent = Sent(monkeypatch)
    c = stable_clients(env, FakeAsg({}), [("latest", env.new, 1)])
    app.handler(cfn_event("Create"), None, stable_cfg(), c)
    assert sent.calls == [("Create", "FAILED", {})]


def test_config_needs_a_source():
    with pytest.raises(ValueError):
        app.Config(env={"ASG_NAMES": "a", "REFRESH_PREFERENCES": "{}"})
    with pytest.raises(ValueError):
        app.Config(env={"ASG_NAMES": "a", "REFRESH_PREFERENCES": "{}", "STABLE_PARAM": STABLE})

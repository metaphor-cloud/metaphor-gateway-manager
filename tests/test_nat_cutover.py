from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

import nat_cutover as app

AZ = "us-east-1a"


@pytest.fixture
def aws():
    with mock_aws():
        yield


class FakeTime:
    """A clock that only moves when the code sleeps, plus a hook to change the
    world (an instance tagging itself) at a chosen sleep."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep or (lambda n: None)

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps += 1
        self.now += s
        self.on_sleep(self.sleeps)


def world():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    sn = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone=AZ)["Subnet"]["SubnetId"]
    egress = ec2.create_network_interface(SubnetId=sn)["NetworkInterface"]["NetworkInterfaceId"]
    return ec2, sn, egress


def launch(ec2, sn):
    return ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1, SubnetId=sn)["Instances"][0]["InstanceId"]


def tag(ec2, iid, state):
    ec2.create_tags(Resources=[iid], Tags=[{"Key": app.STATE_TAG, "Value": state}])


def holder(ec2, eni_id):
    e = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"][0]
    return e.get("Attachment", {}).get("InstanceId")


def cfg(eni_id):
    return app.Config(env={
        "NAT_ASG_NAMES": "asg", "EGRESS_ENI_IDS": eni_id, "READY_TIMEOUT_SECONDS": "60",
        "EGRESS_TIMEOUT_SECONDS": "60", "POLL_SECONDS": "10", "HEARTBEAT_SECONDS": "30",
    }, asg="asg")


def clients_with_mock_asg():
    c = app.Clients()
    c.asg = Mock()
    return c


def detail(iid, transition="LAUNCHING"):
    return {"EC2InstanceId": iid, "LifecycleHookName": "launch-hook", "AutoScalingGroupName": "asg",
            "LifecycleActionToken": "tok", "LifecycleTransition": f"autoscaling:EC2_INSTANCE_{transition}"}


def run(c, conf, iid, t):
    """handler() with injected config, clients and time."""
    hook = app.Hook(conf, c, detail(iid), clock=t.clock)
    result = "ABANDON"
    try:
        app.cutover(conf, c, hook, iid, sleep=t.sleep, clock=t.clock)
        result = "CONTINUE"
    except Exception:
        pass
    hook.complete(result)
    return result


def completed_with(c):
    return c.asg.complete_lifecycle_action.call_args.kwargs["LifecycleActionResult"]


def test_first_launch_attaches_the_egress_eni_and_continues(aws):
    ec2, sn, egress = world()
    new = launch(ec2, sn)
    c = clients_with_mock_asg()
    t = FakeTime(on_sleep=lambda n: tag(ec2, new, "ready") if n == 1 else
                 (tag(ec2, new, "egress") if holder(ec2, egress) == new else None))

    assert run(c, cfg(egress), new, t) == "CONTINUE"
    assert holder(ec2, egress) == new
    e = ec2.describe_network_interfaces(NetworkInterfaceIds=[egress])["NetworkInterfaces"][0]
    assert e["Attachment"]["DeviceIndex"] == 1
    inst = ec2.describe_instances(InstanceIds=[new])["Reservations"][0]["Instances"][0]
    assert app.primary_eni(inst)
    assert completed_with(c) == "CONTINUE"


def test_refresh_moves_the_eni_from_the_old_instance(aws):
    ec2, sn, egress = world()
    old = launch(ec2, sn)
    ec2.attach_network_interface(NetworkInterfaceId=egress, InstanceId=old, DeviceIndex=1)
    new = launch(ec2, sn)
    c = clients_with_mock_asg()
    t = FakeTime(on_sleep=lambda n: tag(ec2, new, "ready") if n == 1 else
                 (tag(ec2, new, "egress") if holder(ec2, egress) == new else None))

    assert run(c, cfg(egress), new, t) == "CONTINUE"
    assert holder(ec2, egress) == new


def test_a_new_instance_that_never_gets_ready_is_abandoned_and_the_eni_stays(aws):
    ec2, sn, egress = world()
    old = launch(ec2, sn)
    ec2.attach_network_interface(NetworkInterfaceId=egress, InstanceId=old, DeviceIndex=1)
    new = launch(ec2, sn)
    c = clients_with_mock_asg()

    assert run(c, cfg(egress), new, FakeTime()) == "ABANDON"
    assert holder(ec2, egress) == old, "the old instance must keep serving"
    assert completed_with(c) == "ABANDON"


def test_egress_never_confirmed_restores_the_eni_to_the_old_instance(aws):
    ec2, sn, egress = world()
    old = launch(ec2, sn)
    ec2.attach_network_interface(NetworkInterfaceId=egress, InstanceId=old, DeviceIndex=1)
    new = launch(ec2, sn)
    c = clients_with_mock_asg()
    t = FakeTime(on_sleep=lambda n: tag(ec2, new, "ready") if n == 1 else None)

    assert run(c, cfg(egress), new, t) == "ABANDON"
    assert holder(ec2, egress) == old


def test_a_redelivered_event_does_not_move_the_eni_again(aws):
    ec2, sn, egress = world()
    new = launch(ec2, sn)
    ec2.attach_network_interface(NetworkInterfaceId=egress, InstanceId=new, DeviceIndex=1)
    tag(ec2, new, "egress")
    c = clients_with_mock_asg()
    c.ec2 = Mock(wraps=c.ec2)

    assert run(c, cfg(egress), new, FakeTime()) == "CONTINUE"
    c.ec2.detach_network_interface.assert_not_called()
    c.ec2.attach_network_interface.assert_not_called()


def test_heartbeats_are_sent_while_waiting(aws):
    ec2, sn, egress = world()
    new = launch(ec2, sn)
    c = clients_with_mock_asg()

    run(c, cfg(egress), new, FakeTime())
    assert c.asg.record_lifecycle_action_heartbeat.call_count >= 1


def test_terminating_events_are_ignored(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(app, "Clients", lambda: fake)
    assert app.handler({"detail": detail("i-x", "TERMINATING")}, None) == {"status": "ignored"}
    fake.asg.complete_lifecycle_action.assert_not_called()


def test_handler_always_completes_even_on_an_unexpected_error(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(app, "Clients", lambda: fake)
    real_config = app.Config
    monkeypatch.setattr(app, "Config", lambda **kw: real_config(env={"NAT_ASG_NAMES": "asg", "EGRESS_ENI_IDS": "eni-1"}, **kw))
    monkeypatch.setattr(app, "cutover", Mock(side_effect=RuntimeError("boom")))
    assert app.handler({"detail": detail("i-x")}, None) == {"status": "ABANDON"}
    assert fake.asg.complete_lifecycle_action.call_args.kwargs["LifecycleActionResult"] == "ABANDON"


# --------------------------------------------------------------------------- #
# A refresh whose replacements keep failing their cutover
# --------------------------------------------------------------------------- #

import datetime as _dt


def _asg_with(refresh_status, abandons_since_start):
    start = _dt.datetime(2026, 9, 30, 12, 0, tzinfo=_dt.timezone.utc)
    acts = [{"StartTime": start + _dt.timedelta(minutes=10 - i), "StatusCode": "Cancelled",
             "Description": "Launching a new EC2 instance: i-x",
             "StatusMessage": "Lifecycle Action with token t was abandoned"} for i in range(abandons_since_start)]
    acts.append({"StartTime": start - _dt.timedelta(minutes=5), "StatusCode": "Cancelled",
                 "Description": "Launching a new EC2 instance: i-old",
                 "StatusMessage": "Lifecycle Action with token t was abandoned"})
    asg = Mock()
    asg.describe_instance_refreshes.return_value = {"InstanceRefreshes": [
        {"InstanceRefreshId": "r-1", "Status": refresh_status, "StartTime": start}]}
    pager = Mock()
    pager.paginate.return_value = [{"Activities": acts}]
    asg.get_paginator.return_value = pager
    return asg


def _clients(asg):
    c = Mock()
    c.asg = asg
    return c


def test_the_first_abandon_of_a_refresh_allows_another_attempt():
    c = _clients(_asg_with("InProgress", 0))
    assert app.stop_failing_refresh(cfg("eni-1"), c, "asg") is False
    c.asg.cancel_instance_refresh.assert_not_called()


def test_the_second_abandon_cancels_the_refresh():
    c = _clients(_asg_with("InProgress", 1))
    assert app.stop_failing_refresh(cfg("eni-1"), c, "asg") is True
    c.asg.cancel_instance_refresh.assert_called_once_with(AutoScalingGroupName="asg")


def test_a_cancelled_refresh_is_published_to_the_update_topic():
    c = _clients(_asg_with("InProgress", 1))
    conf = cfg("eni-1")
    conf.notify_topic_arn = "arn:aws:sns:ap-southeast-2:1:stack-ami-updates"
    app.stop_failing_refresh(conf, c, "asg")
    kw = c.sns.publish.call_args.kwargs
    assert kw["TopicArn"] == conf.notify_topic_arn
    assert "r-1" in kw["Message"] and "reason=refresh_cancelled" in kw["Message"]
    assert len(kw["Subject"]) <= 100


def test_a_refresh_that_is_allowed_another_attempt_publishes_nothing():
    c = _clients(_asg_with("InProgress", 0))
    conf = cfg("eni-1")
    conf.notify_topic_arn = "arn:aws:sns:ap-southeast-2:1:t"
    app.stop_failing_refresh(conf, c, "asg")
    c.sns.publish.assert_not_called()


def test_a_failed_publish_does_not_undo_the_cancellation():
    c = _clients(_asg_with("InProgress", 1))
    c.sns.publish.side_effect = RuntimeError("throttled")
    conf = cfg("eni-1")
    conf.notify_topic_arn = "arn:aws:sns:ap-southeast-2:1:t"
    assert app.stop_failing_refresh(conf, c, "asg") is True
    c.asg.cancel_instance_refresh.assert_called_once()


def test_no_topic_means_no_publish():
    c = _clients(_asg_with("InProgress", 1))
    app.stop_failing_refresh(cfg("eni-1"), c, "asg")
    c.sns.publish.assert_not_called()


def test_abandons_before_the_refresh_started_do_not_count():
    # One abandon predates the refresh in every fixture; only this refresh's count.
    c = _clients(_asg_with("InProgress", 0))
    app.stop_failing_refresh(cfg("eni-1"), c, "asg")
    c.asg.cancel_instance_refresh.assert_not_called()


def test_no_refresh_in_progress_means_nothing_to_cancel():
    # A health replacement with the old gateway gone must keep trying.
    c = _clients(_asg_with("Successful", 5))
    assert app.stop_failing_refresh(cfg("eni-1"), c, "asg") is False
    c.asg.cancel_instance_refresh.assert_not_called()


def test_handler_checks_the_refresh_only_after_an_abandon(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(app, "Clients", lambda: fake)
    real_config = app.Config
    monkeypatch.setattr(app, "Config", lambda **kw: real_config(env={"NAT_ASG_NAMES": "asg", "EGRESS_ENI_IDS": "eni-1"}, **kw))
    stop = Mock()
    monkeypatch.setattr(app, "stop_failing_refresh", stop)

    monkeypatch.setattr(app, "cutover", Mock())
    app.handler({"detail": detail("i-x")}, None)
    stop.assert_not_called()

    monkeypatch.setattr(app, "cutover", Mock(side_effect=app.CutoverFailed("state_ready_timeout")))
    app.handler({"detail": detail("i-x")}, None)
    stop.assert_called_once()


# --------------------------------------------------------------------------- #
# One function, several NAT-only groups
# --------------------------------------------------------------------------- #

def test_each_group_moves_its_own_egress_interface():
    c = app.Config(env={"NAT_ASG_NAMES": "stack-nat-a, stack-nat-b", "EGRESS_ENI_IDS": "eni-a,eni-b"},
                   asg="stack-nat-b")
    assert c.eni_id == "eni-b"
    assert c.egress_enis == {"stack-nat-a": "eni-a", "stack-nat-b": "eni-b"}


def test_unpaired_lists_are_rejected():
    with pytest.raises(ValueError, match="2 entries but EGRESS_ENI_IDS has 1"):
        app.Config(env={"NAT_ASG_NAMES": "stack-nat-a,stack-nat-b", "EGRESS_ENI_IDS": "eni-a"}, asg="stack-nat-a")


def test_a_launch_in_an_unmapped_group_is_abandoned_without_touching_anything(monkeypatch, caplog):
    fake = Mock()
    monkeypatch.setattr(app, "Clients", lambda: fake)
    real_config = app.Config
    monkeypatch.setattr(app, "Config", lambda **kw: real_config(env={"NAT_ASG_NAMES": "other", "EGRESS_ENI_IDS": "eni-1"}, **kw))
    cutover = Mock()
    monkeypatch.setattr(app, "cutover", cutover)
    monkeypatch.setattr(app, "stop_failing_refresh", Mock())

    assert app.handler({"detail": detail("i-x")}, None) == {"status": "ABANDON"}
    cutover.assert_not_called()
    assert fake.asg.complete_lifecycle_action.call_args.kwargs["LifecycleActionResult"] == "ABANDON"
    assert "reason=unknown_group" in caplog.text

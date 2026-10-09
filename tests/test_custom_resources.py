"""Behaviour tests for the gossip secret and EIP cleanup custom resources.

AWS runs against moto; the CloudFormation response PUT is captured instead of
sent.
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import eip_cleanup
import gossip_secret

CONTEXT = SimpleNamespace(log_stream_name="stream")


def event(request_type, **props):
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cfn-response.example/",
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/s/guid",
        "RequestId": "req",
        "LogicalResourceId": "Res",
        "ResourceProperties": props,
    }


@pytest.fixture
def responses():
    sent = []

    def capture(req):
        sent.append(json.loads(req.data))

    with patch("urllib.request.urlopen", side_effect=capture):
        yield sent


@pytest.fixture
def aws():
    with mock_aws():
        yield


# --- gossip_secret -----------------------------------------------------------

def test_create_writes_a_32_byte_secure_string(aws, responses):
    gossip_secret.handler(event("Create", ParameterName="/flow/s/gossip-hmac"), CONTEXT)
    p = boto3.client("ssm").get_parameter(Name="/flow/s/gossip-hmac", WithDecryption=True)["Parameter"]
    assert p["Type"] == "SecureString"
    assert len(base64.b64decode(p["Value"])) == 32
    assert responses[-1]["Status"] == "SUCCESS"


def test_create_keeps_an_existing_key(aws, responses):
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/flow/s/gossip-hmac", Value="existing", Type="SecureString")
    gossip_secret.handler(event("Create", ParameterName="/flow/s/gossip-hmac"), CONTEXT)
    assert ssm.get_parameter(Name="/flow/s/gossip-hmac", WithDecryption=True)["Parameter"]["Value"] == "existing"
    assert responses[-1]["Status"] == "SUCCESS"


def test_update_leaves_the_key_alone(aws, responses):
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/flow/s/gossip-hmac", Value="existing", Type="SecureString")
    gossip_secret.handler(event("Update", ParameterName="/flow/s/gossip-hmac"), CONTEXT)
    assert ssm.get_parameter(Name="/flow/s/gossip-hmac", WithDecryption=True)["Parameter"]["Value"] == "existing"


def test_delete_removes_the_key_and_tolerates_it_missing(aws, responses):
    gossip_secret.handler(event("Create", ParameterName="/flow/s/gossip-hmac"), CONTEXT)
    gossip_secret.handler(event("Delete", ParameterName="/flow/s/gossip-hmac"), CONTEXT)
    gossip_secret.handler(event("Delete", ParameterName="/flow/s/gossip-hmac"), CONTEXT)
    assert boto3.client("ssm").describe_parameters()["Parameters"] == []
    assert [r["Status"] for r in responses] == ["SUCCESS", "SUCCESS", "SUCCESS"]


def test_an_ssm_failure_reports_failed_and_logs_why(aws, responses, capsys):
    with patch("boto3.client", side_effect=RuntimeError("no ssm")):
        gossip_secret.handler(event("Create", ParameterName="/flow/s/gossip-hmac"), CONTEXT)
    assert responses[-1]["Status"] == "FAILED"
    assert "reason=ssm_error" in capsys.readouterr().out


# --- eip_cleanup -------------------------------------------------------------

def network():
    ec2 = boto3.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.0.0/24")["Subnet"]["SubnetId"]
    return ec2, subnet


def tags(stack, managed=True):
    t = [{"Key": "metaphor:stack-name", "Value": stack}]
    if managed:
        t.append({"Key": "metaphor:managed", "Value": "true"})
    return t


def test_delete_releases_tagged_eips_and_deletes_tagged_enis_only(aws, responses):
    ec2, subnet = network()
    mine_eni = ec2.create_network_interface(
        SubnetId=subnet, TagSpecifications=[{"ResourceType": "network-interface", "Tags": tags("s")}],
    )["NetworkInterface"]["NetworkInterfaceId"]
    other_eni = ec2.create_network_interface(
        SubnetId=subnet, TagSpecifications=[{"ResourceType": "network-interface", "Tags": tags("other")}],
    )["NetworkInterface"]["NetworkInterfaceId"]
    mine_eip = ec2.allocate_address(
        Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": tags("s", managed=False)}],
    )["AllocationId"]
    ec2.associate_address(AllocationId=mine_eip, NetworkInterfaceId=mine_eni)
    caller_eip = ec2.allocate_address(Domain="vpc")["AllocationId"]

    eip_cleanup.handler(event("Delete", StackName="s"), CONTEXT)

    enis = {n["NetworkInterfaceId"] for n in ec2.describe_network_interfaces()["NetworkInterfaces"]}
    eips = {a["AllocationId"] for a in ec2.describe_addresses()["Addresses"]}
    assert mine_eni not in enis and other_eni in enis
    assert mine_eip not in eips and caller_eip in eips
    assert responses[-1]["Status"] == "SUCCESS"


def test_create_and_update_touch_nothing(aws, responses):
    ec2, subnet = network()
    eni = ec2.create_network_interface(
        SubnetId=subnet, TagSpecifications=[{"ResourceType": "network-interface", "Tags": tags("s")}],
    )["NetworkInterface"]["NetworkInterfaceId"]
    eip_cleanup.handler(event("Create", StackName="s"), CONTEXT)
    eip_cleanup.handler(event("Update", StackName="s"), CONTEXT)
    assert ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])["NetworkInterfaces"]
    assert [r["Status"] for r in responses] == ["SUCCESS", "SUCCESS"]


def test_a_failed_sweep_still_reports_success_and_logs_why(aws, responses, capsys):
    with patch("boto3.client", side_effect=RuntimeError("no ec2")):
        eip_cleanup.handler(event("Delete", StackName="s"), CONTEXT)
    assert responses[-1]["Status"] == "SUCCESS"
    assert "reason=sweep_failed" in capsys.readouterr().out

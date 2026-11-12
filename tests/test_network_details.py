"""Behaviour tests for the network_details custom resource.

AWS runs against moto; the CloudFormation response PUT is captured instead of
sent.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import network_details

CONTEXT = SimpleNamespace(log_stream_name="stream")


def event(request_type, physical=None, **props):
    e = {
        "RequestType": request_type,
        "ResponseURL": "https://cfn-response.example/",
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/s/guid",
        "RequestId": "req",
        "LogicalResourceId": "Res",
        "ResourceProperties": props,
    }
    if physical:
        e["PhysicalResourceId"] = physical
    return e


def run(e):
    network_details.handler(e, CONTEXT, sleep=lambda _s: None)


@pytest.fixture
def responses():
    sent = []

    def capture(req):
        sent.append(json.loads(req.data))

    with patch("urllib.request.urlopen", side_effect=capture):
        yield sent


@pytest.fixture
def ec2():
    with mock_aws():
        yield boto3.client("ec2")


def entries(ec2, pl_id):
    return sorted(e["Cidr"] for e in ec2.get_managed_prefix_list_entries(PrefixListId=pl_id)["Entries"])


def max_entries(ec2, pl_id):
    return ec2.describe_managed_prefix_lists(PrefixListIds=[pl_id])["PrefixLists"][0]["MaxEntries"]


# --- VPC ---------------------------------------------------------------------

def test_vpc_list_holds_every_associated_block(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ec2.associate_vpc_cidr_block(VpcId=vpc, CidrBlock="10.1.0.0/16")

    run(event("Create", VpcId=vpc, Name="s-vpc"))

    r = responses[-1]
    assert r["Status"] == "SUCCESS"
    pl = r["PhysicalResourceId"]
    assert pl.startswith("pl-") and r["Data"]["PrefixListId"] == pl
    assert entries(ec2, pl) == ["10.0.0.0/16", "10.1.0.0/16"]
    assert r["Data"]["Cidrs"] == "10.0.0.0/16,10.1.0.0/16"
    # Sized for the default VPC block quota, not just today's count.
    assert max_entries(ec2, pl) == network_details.VPC_MIN_ENTRIES
    # The tag the function's IAM policy scopes modify and delete to.
    tags = ec2.describe_managed_prefix_lists(PrefixListIds=[pl])["PrefixLists"][0]["Tags"]
    assert network_details.MANAGED_TAG in tags


def test_vpc_list_skips_a_disassociated_block(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    assoc = ec2.associate_vpc_cidr_block(VpcId=vpc, CidrBlock="10.1.0.0/16")["CidrBlockAssociation"]
    ec2.disassociate_vpc_cidr_block(AssociationId=assoc["AssociationId"])

    run(event("Create", VpcId=vpc, Name="s-vpc"))

    assert entries(ec2, responses[-1]["PhysicalResourceId"]) == ["10.0.0.0/16"]


def test_update_follows_a_new_vpc_block_in_place(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    run(event("Create", VpcId=vpc, Name="s-vpc"))
    pl = responses[-1]["PhysicalResourceId"]

    ec2.associate_vpc_cidr_block(VpcId=vpc, CidrBlock="10.2.0.0/16")
    run(event("Update", physical=pl, VpcId=vpc, Name="s-vpc"))

    # Same list, so the security groups that reference it are not replaced.
    assert responses[-1]["Status"] == "SUCCESS"
    assert responses[-1]["PhysicalResourceId"] == pl
    assert entries(ec2, pl) == ["10.0.0.0/16", "10.2.0.0/16"]


# --- subnets -----------------------------------------------------------------

def subnets(ec2, vpc, cidrs):
    return [ec2.create_subnet(VpcId=vpc, CidrBlock=c)["Subnet"]["SubnetId"] for c in cidrs]


def test_subnet_list_from_a_comma_separated_string(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ids = subnets(ec2, vpc, ["10.0.1.0/24", "10.0.2.0/24"])

    run(event("Create", SubnetIds=",".join(ids), Name="s-gwlb"))

    pl = responses[-1]["PhysicalResourceId"]
    assert entries(ec2, pl) == ["10.0.1.0/24", "10.0.2.0/24"]
    assert max_entries(ec2, pl) == 2


def test_update_grows_the_list_before_adding_entries(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ids = subnets(ec2, vpc, ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"])
    run(event("Create", SubnetIds=ids[:1], Name="s-gwlb"))
    pl = responses[-1]["PhysicalResourceId"]
    assert max_entries(ec2, pl) == 1

    # EC2 rejects a resize and an entry change in one call, and moto neither
    # enforces that nor applies MaxEntries, so record what reaches the API.
    real_client = boto3.client
    modify_calls = []

    def client(name, *a, **kw):
        c = real_client(name, *a, **kw)
        real_modify = c.modify_managed_prefix_list

        def modify(**kwargs):
            modify_calls.append(kwargs)
            return real_modify(**kwargs)

        c.modify_managed_prefix_list = modify
        return c

    with patch.object(network_details.boto3, "client", side_effect=client):
        run(event("Update", physical=pl, SubnetIds=ids, Name="s-gwlb"))

    assert responses[-1]["Status"] == "SUCCESS"
    assert [sorted(k for k in c if k not in ("PrefixListId", "CurrentVersion")) for c in modify_calls] == [
        ["MaxEntries"],
        ["AddEntries", "PrefixListName"],
    ]
    assert modify_calls[0]["MaxEntries"] == 3
    assert entries(ec2, pl) == ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]


def test_update_removes_a_subnet_that_left_the_list(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ids = subnets(ec2, vpc, ["10.0.1.0/24", "10.0.2.0/24"])
    run(event("Create", SubnetIds=ids, Name="s-gwlb"))
    pl = responses[-1]["PhysicalResourceId"]

    run(event("Update", physical=pl, SubnetIds=ids[1:], Name="s-gwlb"))

    assert entries(ec2, pl) == ["10.0.2.0/24"]


def test_a_missing_subnet_fails_the_create(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ids = subnets(ec2, vpc, ["10.0.1.0/24"])

    run(event("Create", SubnetIds=ids + ["subnet-0000000000000dead"], Name="s-gwlb"))

    assert responses[-1]["Status"] == "FAILED"
    assert ec2.describe_managed_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": ["s-gwlb"]}])["PrefixLists"] == []


@pytest.mark.parametrize("props", [{}, {"VpcId": "vpc-1", "SubnetIds": "subnet-1"}])
def test_exactly_one_source_is_required(ec2, responses, props):
    run(event("Create", Name="s", **props))
    assert responses[-1]["Status"] == "FAILED"
    assert "exactly one" in responses[-1]["Reason"]


# --- delete ------------------------------------------------------------------

def test_delete_removes_the_list(ec2, responses):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    run(event("Create", VpcId=vpc, Name="s-vpc"))
    pl = responses[-1]["PhysicalResourceId"]

    run(event("Delete", physical=pl, VpcId=vpc, Name="s-vpc"))

    assert responses[-1]["Status"] == "SUCCESS"
    remaining = [p for p in ec2.describe_managed_prefix_lists()["PrefixLists"]
                 if p["PrefixListId"] == pl and p["State"] != "delete-complete"]
    assert remaining == []


def test_delete_after_a_failed_create_touches_nothing(ec2, responses):
    # A failed create reports the log stream as its physical id.
    with patch.object(network_details, "delete") as d:
        run(event("Delete", physical="stream", VpcId="vpc-1", Name="s-vpc"))
    d.assert_not_called()
    assert responses[-1]["Status"] == "SUCCESS"


def test_a_failed_delete_does_not_block_the_stack(ec2, responses):
    run(event("Delete", physical="pl-00000000000000000", VpcId="vpc-1", Name="s-vpc"))
    assert responses[-1]["Status"] == "SUCCESS"

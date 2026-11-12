"""Custom resource: a managed prefix list that tracks a VPC's or subnets' CIDRs.

Security groups can only name an address range, and a CIDR typed into a stack
parameter is never checked against the network it describes: a typo, or a VPC
that later gains a secondary CIDR, silently widens or narrows every rule that
uses it. This resource reads the real ranges from EC2 instead and keeps them in
a managed prefix list that the security groups reference.

Properties (exactly one of VpcId or SubnetIds):
  VpcId       every IPv4 CIDR block currently associated with the VPC
  SubnetIds   the IPv4 CIDR block of each subnet (a list, or a comma-separated
              string)
  Name        the prefix list's name

Returns PrefixListId (also the physical id) and Cidrs, comma-separated. An
update rewrites the same list in place, growing it first if needed, so the
security groups that reference it are never replaced. Delete removes the list;
a failed delete is logged and reported as SUCCESS, because leaving an unused
prefix list behind costs nothing and must not block the stack delete.
"""

import json
import time
import urllib.request

import boto3

# A VPC holds up to five IPv4 CIDR blocks by default. Sizing the list to that
# up front means a VPC that gains a block later only needs its entries changed.
VPC_MIN_ENTRIES = 5
# Every list this creates carries this tag, so the function's IAM policy can
# allow modify and delete on its own lists and nothing else.
MANAGED_TAG = {"Key": "metaphor:managed-by", "Value": "network-details"}
POLL_SECONDS = 2
POLL_ATTEMPTS = 60


def cfn_send(event, context, status, data=None, physical_id=None, reason=None):
    body = json.dumps({
        "Status": status,
        "Reason": reason or ("See " + context.log_stream_name),
        "PhysicalResourceId": physical_id or event.get("PhysicalResourceId", context.log_stream_name),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, headers={"content-type": ""}, method="PUT")
    urllib.request.urlopen(req)


def subnet_ids(props):
    raw = props.get("SubnetIds") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [s.strip() for s in raw if s and s.strip()]


def wanted_cidrs(ec2, props):
    """The CIDRs the list should hold, sorted, plus the size to create it at."""
    vpc_id = (props.get("VpcId") or "").strip()
    subnets = subnet_ids(props)
    if bool(vpc_id) == bool(subnets):
        raise ValueError("set exactly one of VpcId or SubnetIds")

    if vpc_id:
        vpc = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
        # Only blocks in the associated state are routable. A block being
        # disassociated is on its way out and must not stay allowed.
        cidrs = [a["CidrBlock"] for a in vpc.get("CidrBlockAssociationSet", [])
                 if a.get("CidrBlockState", {}).get("State") == "associated"]
        floor = VPC_MIN_ENTRIES
    else:
        found = ec2.describe_subnets(SubnetIds=subnets)["Subnets"]
        missing = set(subnets) - {s["SubnetId"] for s in found}
        if missing:
            raise ValueError(f"subnets not found: {', '.join(sorted(missing))}")
        cidrs = [s["CidrBlock"] for s in found]
        floor = len(subnets)

    cidrs = sorted(set(cidrs))
    if not cidrs:
        raise ValueError("no IPv4 CIDR blocks found")
    return cidrs, max(floor, len(cidrs))


def wait_settled(ec2, pl_id, sleep=time.sleep):
    """Wait for the list to leave a *-in-progress state. Raises on a failed one."""
    for _ in range(POLL_ATTEMPTS):
        pl = ec2.describe_managed_prefix_lists(PrefixListIds=[pl_id])["PrefixLists"][0]
        state = pl["State"]
        if state.endswith("-complete"):
            return pl
        if state.endswith("-failed"):
            raise RuntimeError(f"{pl_id} entered {state}: {pl.get('StateMessage', '')}")
        sleep(POLL_SECONDS)
    raise TimeoutError(f"{pl_id} still {state} after {POLL_ATTEMPTS * POLL_SECONDS}s")


def current_cidrs(ec2, pl_id):
    entries = ec2.get_managed_prefix_list_entries(PrefixListId=pl_id)["Entries"]
    return sorted(e["Cidr"] for e in entries)


def create(ec2, name, cidrs, size, sleep=time.sleep):
    pl = ec2.create_managed_prefix_list(
        PrefixListName=name,
        AddressFamily="IPv4",
        MaxEntries=size,
        Entries=[{"Cidr": c} for c in cidrs],
        TagSpecifications=[{"ResourceType": "prefix-list", "Tags": [MANAGED_TAG, {"Key": "Name", "Value": name}]}],
    )["PrefixList"]
    wait_settled(ec2, pl["PrefixListId"], sleep)
    print(f"created {pl['PrefixListId']} {name} with {cidrs}")
    return pl["PrefixListId"]


def update(ec2, pl_id, name, cidrs, size, sleep=time.sleep):
    pl = wait_settled(ec2, pl_id, sleep)

    # EC2 refuses a resize and an entry change in the same call, so grow first.
    if size > pl["MaxEntries"]:
        ec2.modify_managed_prefix_list(PrefixListId=pl_id, CurrentVersion=pl["Version"], MaxEntries=size)
        pl = wait_settled(ec2, pl_id, sleep)
        print(f"resized {pl_id} to {size} entries")

    have = set(current_cidrs(ec2, pl_id))
    add = sorted(set(cidrs) - have)
    remove = sorted(have - set(cidrs))
    if add or remove or pl["PrefixListName"] != name:
        kwargs = {"PrefixListId": pl_id, "CurrentVersion": pl["Version"], "PrefixListName": name}
        if add:
            kwargs["AddEntries"] = [{"Cidr": c} for c in add]
        if remove:
            kwargs["RemoveEntries"] = [{"Cidr": c} for c in remove]
        ec2.modify_managed_prefix_list(**kwargs)
        wait_settled(ec2, pl_id, sleep)
        print(f"updated {pl_id}: added {add}, removed {remove}")
    return pl_id


def delete(ec2, pl_id):
    try:
        ec2.delete_managed_prefix_list(PrefixListId=pl_id)
        print(f"deleted {pl_id}")
    except Exception as e:
        print(f"delete {pl_id} failed (reason=prefix_list_delete_failed), leaving it: {type(e).__name__}: {e}")


def is_prefix_list_id(value):
    return isinstance(value, str) and value.startswith("pl-")


def handler(event, context, sleep=time.sleep):
    props = event.get("ResourceProperties", {})
    physical = event.get("PhysicalResourceId")
    ec2 = boto3.client("ec2")

    if event["RequestType"] == "Delete":
        # A create that failed before a list existed reports the log stream as
        # its physical id; there is nothing to delete then.
        if is_prefix_list_id(physical):
            delete(ec2, physical)
        cfn_send(event, context, "SUCCESS")
        return

    try:
        name = props["Name"]
        cidrs, size = wanted_cidrs(ec2, props)
        if event["RequestType"] == "Update" and is_prefix_list_id(physical):
            pl_id = update(ec2, physical, name, cidrs, size, sleep)
        else:
            pl_id = create(ec2, name, cidrs, size, sleep)
        cfn_send(event, context, "SUCCESS", {"PrefixListId": pl_id, "Cidrs": ",".join(cidrs)}, physical_id=pl_id)
    except Exception as e:
        print(f"network details {event['RequestType']} failed (reason=network_details_error): {type(e).__name__}: {e}")
        cfn_send(event, context, "FAILED", reason=f"{type(e).__name__}: {e}")

import os
import sys

# The functions under test are the modules in ../src, one per entrypoint.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

# FORCE this environment, do not setdefault it. With a region or credentials
# already exported (a CI runner, a developer profile), setdefault would keep them:
# a module's boto3 clients would resolve that region while the tests' helpers use
# us-east-1, splitting the moto backends so one side's resources are invisible to
# the other. Dummy credentials also mean anything moto failed to intercept cannot
# reach real AWS with the caller's permissions.
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

# A profile would reintroduce a region (and credentials) from the config file.
os.environ.pop("AWS_PROFILE", None)

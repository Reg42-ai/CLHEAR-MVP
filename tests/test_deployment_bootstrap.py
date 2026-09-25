"""Security boundaries of the owner-approved deployment enrollment artifact."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra/bootstrap/clhear-deployment-role.json"
ACCOUNT = "730649732189"


def _role():
    template = json.loads(TEMPLATE.read_text())
    assert set(template["Resources"]) == {"DeploymentRole", "StableViewerMetadataPolicy", "WebServiceRollPolicy"}
    resources = [r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(resources) == 1, "Enrollment must not silently adopt runtime resources"
    assert resources[0]["Type"] == "AWS::IAM::Role"
    return resources[0]["Properties"]


def _items(value):
    return value if isinstance(value, list) else [value]


def _grants():
    for policy in _role()["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            if statement["Effect"] == "Allow":
                yield statement


def test_enrollment_creates_only_deployment_identity():
    role = _role()
    assert role["RoleName"] == "clhear-github-deploy"
    assert role["MaxSessionDuration"] == 10800
    assert not role.get("ManagedPolicyArns"), "Review explicit inline grants, not changing managed policies"
    total_inline_size = sum(
        len(json.dumps(p["PolicyDocument"], separators=(",", ":")))
        for p in role["Policies"]
    )
    assert total_inline_size <= 10240, "The IAM quota is shared by all role inline policies"


def test_oidc_trust_requires_exact_repository_environment_and_audience():
    trust = _role()["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    grant = trust[0]
    assert grant["Effect"] == "Allow"
    assert _items(grant["Action"]) == ["sts:AssumeRoleWithWebIdentity"]
    assert grant["Principal"] == {
        "Federated": f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
    }
    assert grant["Condition"] == {"StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:Reg42-ai/CLHEAR-MVP:environment:clhear-l1",
    }}
    workflow = (ROOT / ".github/workflows/deploy-l1.yml").read_text()
    assert "environment: clhear-l1" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow


def test_deployer_cannot_provision_iam_or_pass_another_products_roles():
    iam_grants = [(action, grant) for grant in _grants()
                  for action in _items(grant["Action"]) if action.lower().startswith("iam:")]
    assert {a for a, _ in iam_grants} == {"iam:PassRole", "iam:SimulatePrincipalPolicy"}
    for action, grant in iam_grants:
        if action == "iam:PassRole":
            assert set(_items(grant["Resource"])) == {
                f"arn:aws:iam::{ACCOUNT}:role/clhear-worker-task",
                f"arn:aws:iam::{ACCOUNT}:role/clhear-worker-execution",
            }
            assert grant["Condition"]["StringEquals"]["iam:PassedToService"] == "ecs-tasks.amazonaws.com"
        else:
            assert _items(grant["Resource"]) == [f"arn:aws:iam::{ACCOUNT}:role/clhear-webui"]


def test_deployer_has_no_direct_database_mutation_or_iam_administration_api_grants():
    actions = {action for grant in _grants() for action in _items(grant["Action"])}
    assert "*" not in actions
    assert not any("*" in action for action in actions)
    assert {a for a in actions if a.startswith("rds:")} == {"rds:DescribeDBClusters"}
    assert not any(a.startswith(("rds-data:", "rds-db:", "cloudformation:", "organizations:")) for a in actions)
    assert {a for a in actions if a.startswith("ssm:")} == {"ssm:GetParameter"}
    for grant in _grants():
        if "ssm:GetParameter" in _items(grant["Action"]):
            assert _items(grant["Resource"]) == [
                f"arn:aws:ssm:us-east-1:{ACCOUNT}:parameter/clhear/DATABASE_URL"
            ]


def test_optional_metadata_policy_matches_preview_bootstrap_without_inline_quota_growth():
    template = json.loads(TEMPLATE.read_text())
    preview = json.loads((ROOT / "infra/bootstrap/clhear-preview.json").read_text())
    policy = template["Resources"]["StableViewerMetadataPolicy"]
    assert template["Parameters"]["IncludeStableCodeMetadataPolicy"]["Default"] == "false"
    assert policy["Type"] == "AWS::IAM::ManagedPolicy" and policy["Condition"] == "IncludeStableMetadata"
    assert policy["Properties"]["Roles"] == [{"Ref": "DeploymentRole"}]
    assert policy["Properties"]["PolicyDocument"] == preview["Resources"]["StableViewerMetadataPolicy"]["Properties"]["PolicyDocument"]
    assert policy["Properties"]["ManagedPolicyName"] == "clhear-viewer-code-metadata"
    assert len(json.dumps(policy["Properties"]["PolicyDocument"], separators=(",", ":"))) <= 6144
    assert not _role().get("ManagedPolicyArns"), "Only the optional policy resource attaches this grant"


def test_optional_web_service_roll_policy_is_off_by_default_and_names_only_the_web_service():
    template = json.loads(TEMPLATE.read_text())
    policy = template["Resources"]["WebServiceRollPolicy"]
    assert template["Parameters"]["IncludeWebServiceRollPolicy"]["Default"] == "false"
    assert policy["Type"] == "AWS::IAM::ManagedPolicy" and policy["Condition"] == "IncludeWebServiceRoll"
    assert policy["Properties"]["Roles"] == [{"Ref": "DeploymentRole"}]
    assert policy["Properties"]["ManagedPolicyName"] == "clhear-web-service-roll"
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    actions = {a for st in statements for a in _items(st["Action"])}
    assert actions == {"ecs:DescribeServices", "ecs:UpdateService", "ecs:RegisterTaskDefinition",
                       "ecs:ListTagsForResource", "ecs:TagResource", "iam:PassRole"}
    resources = {r for st in statements for r in _items(st["Resource"])}
    assert resources == {
        f"arn:aws:ecs:us-east-1:{ACCOUNT}:service/clhear-cluster/clhear-webui-service",
        f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/clhear-webui-service:*",
        f"arn:aws:iam::{ACCOUNT}:role/clhear-webui-service-task",
    }
    for statement in statements:
        if "iam:PassRole" in _items(statement["Action"]):
            assert statement["Condition"]["StringEquals"]["iam:PassedToService"] == "ecs-tasks.amazonaws.com"
        if "ecs:TagResource" in _items(statement["Action"]):
            assert statement["Condition"]["StringEquals"]["ecs:CreateAction"] == "RegisterTaskDefinition"
    assert not _role().get("ManagedPolicyArns"), "Only the optional policy resource attaches this grant"

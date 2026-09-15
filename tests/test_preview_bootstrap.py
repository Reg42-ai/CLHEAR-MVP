"""Review boundaries of the owner-created hosted preview; no AWS calls."""
import fnmatch
import json
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((ROOT / "infra/bootstrap/clhear-preview.json").read_text())
RESOURCES = TEMPLATE["Resources"]
FUNCTION_ARN = "arn:aws:lambda:us-east-1:730649732189:function:clhear-preview-webui"
BUCKET_ARN = "arn:aws:s3:::clhear-deploy-730649732189"


def items(value):
    return value if isinstance(value, list) else [value]


def grants(role):
    return [statement for policy in RESOURCES[role]["Properties"]["Policies"]
            for statement in policy["PolicyDocument"]["Statement"] if statement["Effect"] == "Allow"]


def allows(role, action, resource, *, encryption="AES256", prefix=None):
    """Evaluate this template's positive scopes, including its S3 encryption guard."""
    for grant in grants(role):
        if (any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in items(grant["Action"]))
                and any(fnmatch.fnmatchcase(resource, pattern) for pattern in items(grant["Resource"]))):
            expected = grant.get("Condition", {}).get("StringEquals", {}).get("s3:x-amz-server-side-encryption")
            expected_prefix = grant.get("Condition", {}).get("StringEquals", {}).get("s3:prefix")
            if (expected is None or encryption == expected) and (expected_prefix is None or prefix == expected_prefix):
                return True
    return False


def test_preview_resources_cannot_adopt_stable_compute_or_data():
    assert {r["Type"] for r in RESOURCES.values()} == {
        "AWS::ApiGatewayV2::Api", "AWS::ApiGatewayV2::Integration", "AWS::ApiGatewayV2::Route",
        "AWS::ApiGatewayV2::Stage", "AWS::Lambda::Function", "AWS::Lambda::Permission",
        "AWS::Cognito::UserPoolClient", "AWS::Cognito::UserPoolUICustomizationAttachment",
        "AWS::Cognito::ManagedLoginBranding", "AWS::SecretsManager::Secret", "AWS::Logs::LogGroup", "AWS::IAM::Role",
        "AWS::IAM::ManagedPolicy",
    }
    functions = [r["Properties"] for r in RESOURCES.values() if r["Type"] == "AWS::Lambda::Function"]
    assert len(functions) == 1 and functions[0]["FunctionName"] == "clhear-preview-webui"
    assert not functions[0].get("VpcConfig") and "ReservedConcurrentExecutions" not in functions[0]
    assert not any("FunctionUrl" in r["Type"] or "UserPool" == r["Type"].split("::")[-1] for r in RESOURCES.values())
    assert all(r["Condition"] in {"SupportedDeployment", "UseClassicLogin", "UseManagedLogin", "IncludeStableMetadata"} for r in RESOURCES.values())
    assert all(o["Condition"] in {"SupportedDeployment", "IncludeStableMetadata"} for o in TEMPLATE["Outputs"].values())
    assert TEMPLATE["Conditions"]["SupportedDeployment"] == {"Fn::And": [
        {"Fn::Equals": [{"Ref": "AWS::AccountId"}, "730649732189"]},
        {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-east-1"]},
    ]}


def test_bootstrap_uses_versioned_complete_code_and_an_independent_session_secret():
    function = RESOURCES["PreviewFunction"]["Properties"]
    assert function["Code"] == {"S3Bucket": {"Ref": "DeploymentBucket"}, "S3Key": {"Ref": "InitialCodeKey"},
                                 "S3ObjectVersion": {"Ref": "InitialCodeVersion"}}
    assert function["Runtime"] == "python3.12" and function["Architectures"] == ["x86_64"]
    assert function["Handler"] == "app.clhear.lambda_web.handler"
    env = function["Environment"]["Variables"]
    assert env["CLHEAR_PREVIEW_MODE"] == env["CLHEAR_RESTRICTED_ACCESS"] == "true"
    assert env["CLHEAR_AUTH_DEBUG"] == "false"
    assert env["CLHEAR_DB_S3_URI"] == "s3://clhear-deploy-730649732189/webui/l1/candidate.db"
    assert env["CLHEAR_SESSION_SECRET"] == {"Fn::Sub": "{{resolve:secretsmanager:${PreviewSessionSecret}:SecretString:session_secret}}"}
    assert env["CLHEAR_REVIEWER_EMAILS"] == {"Ref": "ApprovedReviewerEmails"}
    assert TEMPLATE["Parameters"]["ApprovedReviewerEmails"]["NoEcho"] is True
    assert "DATABASE_URL" not in env
    assert all(env[k] == "" for k in ("CLHEAR_EVENTS_QUEUE_URL", "CLHEAR_SNAPSHOT_S3_URI", "CLHEAR_APP_KEYS", "CLHEAR_SES_SENDER"))
    secret = RESOURCES["PreviewSessionSecret"]
    assert secret["Properties"]["GenerateSecretString"]["PasswordLength"] >= 64
    assert secret["DeletionPolicy"] == secret["UpdateReplacePolicy"] == "Retain"
    assert "resolve:secretsmanager" not in json.dumps(TEMPLATE["Outputs"])


@pytest.mark.parametrize("key,valid", [
    ("webui/deployments/" + "a" * 40 + "/1-1/webui.zip", True),
    ("webui/previews/123-1/webui.zip", True),
    ("webui/l1/candidate.db", False), ("webui/previews/../webui.zip", False),
    ("webui/previews/123-1/previous.zip", False), ("webui/previews/123-1/webui.zip\n", False),
])
def test_bootstrap_key_rejects_corpus_and_unreviewed_path_shapes(key, valid):
    assert bool(re.fullmatch(TEMPLATE["Parameters"]["InitialCodeKey"]["AllowedPattern"], key)) is valid


@pytest.mark.parametrize("version,valid", [("abc.A+/=_-", True), ("null", False), ("", False), ("x\n", False)])
def test_bootstrap_requires_an_actual_object_version(version, valid):
    assert bool(re.fullmatch(TEMPLATE["Parameters"]["InitialCodeVersion"]["AllowedPattern"], version)) is valid


def test_runtime_can_only_read_current_candidate_and_write_its_own_logs():
    actions = {a for g in grants("PreviewRuntimeRole") for a in items(g["Action"])}
    assert actions == {"s3:GetObject", "logs:CreateLogStream", "logs:PutLogEvents"}
    assert allows("PreviewRuntimeRole", "s3:GetObject", BUCKET_ARN + "/webui/l1/candidate.db")
    for action, path in [("s3:PutObject", "/webui/l1/candidate.db"), ("s3:GetObjectVersion", "/webui/l1/candidate.db"),
                         ("s3:GetObject", "/releases/latest.json"), ("s3:GetObject", "/webui/old.db")]:
        assert not allows("PreviewRuntimeRole", action, BUCKET_ARN + path)
    assert not allows("PreviewRuntimeRole", "logs:PutLogEvents", "arn:aws:logs:us-east-1:730649732189:log-group:/aws/lambda/clhear-webui:log-stream:x")


def test_preview_can_stage_the_current_snapshot_without_removing_the_previous_copy():
    # S3 metadata observed 2026-09-15; refresh downloads .new before os.replace.
    # Preserve space for both complete copies plus temporary-file overhead.
    snapshot_bytes = 669_437_952
    capacity = RESOURCES["PreviewFunction"]["Properties"]["EphemeralStorage"]["Size"] * 1024 * 1024
    assert capacity >= 2 * snapshot_bytes + 64 * 1024 * 1024
    assert RESOURCES["PreviewFunction"]["Properties"]["MemorySize"] >= 1024


def test_preview_deployment_trust_is_exact_and_does_not_grant_configuration_or_live_operations():
    role = RESOURCES["PreviewDeploymentRole"]["Properties"]
    assert role["RoleName"] == "clhear-github-preview" and role["MaxSessionDuration"] == 3600
    assert role["AssumeRolePolicyDocument"]["Statement"] == [{
        "Effect": "Allow", "Principal": {"Federated": "arn:aws:iam::730649732189:oidc-provider/token.actions.githubusercontent.com"},
        "Action": "sts:AssumeRoleWithWebIdentity", "Condition": {"StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:Reg42-ai/CLHEAR-MVP:environment:clhear-preview",
        }},
    }]
    actions = {a for g in grants("PreviewDeploymentRole") for a in items(g["Action"])}
    assert {a for a in actions if a.startswith("lambda:")} == {
        "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetFunctionConcurrency",
        "lambda:UpdateFunctionCode", "lambda:InvokeFunction",
    }
    assert not any(a.startswith(("iam:", "ecs:", "sqs:", "rds", "secretsmanager:", "ssm:", "cognito-idp:", "cloudformation:")) for a in actions)
    assert not any("*" in a for a in actions)
    assert allows("PreviewDeploymentRole", "lambda:UpdateFunctionCode", FUNCTION_ARN)
    assert not allows("PreviewDeploymentRole", "lambda:UpdateFunctionCode", FUNCTION_ARN.replace("preview-", ""))
    assert not allows("PreviewDeploymentRole", "lambda:UpdateFunctionConfiguration", FUNCTION_ARN)


def test_deployment_artifact_scope_reads_main_code_but_writes_only_encrypted_preview_zips():
    role = "PreviewDeploymentRole"
    assert allows(role, "s3:GetObject", BUCKET_ARN + "/deployments/l1/viewer-release.json")
    assert allows(role, "s3:GetObjectVersion", BUCKET_ARN + "/webui/deployments/main/123-1/webui.zip")
    assert allows(role, "s3:PutObject", BUCKET_ARN + "/webui/previews/123-1/previous.zip")
    assert not allows(role, "s3:PutObject", BUCKET_ARN + "/webui/previews/123-1/previous.zip", encryption=None)
    for path in ("/deployments/l1/viewer-release.json", "/webui/deployments/main/123-1/webui.zip",
                 "/webui/l1/candidate.db", "/releases/latest.json", "/webui/previews/123-1/state.db"):
        assert not allows(role, "s3:PutObject", BUCKET_ARN + path)
    assert not allows(role, "s3:GetObject", BUCKET_ARN + "/webui/l1/candidate.db")
    assert allows(role, "s3:ListBucket", BUCKET_ARN, prefix="deployments/l1/viewer-release.json")
    for prefix in (None, "", "deployments/l1/", "releases/", "webui/l1/candidate.db"):
        assert not allows(role, "s3:ListBucket", BUCKET_ARN, prefix=prefix)


def test_cognito_callback_and_api_integration_do_not_create_a_dependency_cycle():
    api = RESOURCES["PreviewApi"]["Properties"]
    assert api["ProtocolType"] == "HTTP" and "Target" not in api and "Body" not in api
    client = RESOURCES["PreviewCognitoClient"]["Properties"]
    assert client["UserPoolId"] == {"Ref": "CognitoUserPoolId"}
    assert client["GenerateSecret"] is False and client["AllowedOAuthFlows"] == ["code"]
    assert client["AllowedOAuthFlowsUserPoolClient"] is True
    assert client["CallbackURLs"] == [{"Fn::Sub": "${PreviewApi.ApiEndpoint}/auth/cognito/callback"}]
    assert "aws.cognito.signin.user.admin" not in client["AllowedOAuthScopes"]
    assert RESOURCES["PreviewIntegration"]["Properties"]["PayloadFormatVersion"] == "2.0"
    permission = RESOURCES["PreviewInvokePermission"]["Properties"]
    assert permission["FunctionName"] == {"Ref": "PreviewFunction"}
    assert permission["Principal"] == "apigateway.amazonaws.com" and permission["SourceAccount"] == "730649732189"

    def references(value):
        if isinstance(value, list):
            return set().union(*(references(x) for x in value))
        if not isinstance(value, dict):
            return set()
        result = set()
        if "Ref" in value:
            result.add(value["Ref"])
        if "Fn::GetAtt" in value:
            result.add(value["Fn::GetAtt"][0])
        if isinstance(value.get("Fn::Sub"), str):
            result.update(v.split(".")[0] for v in re.findall(r"\$\{([^}]+)\}", value["Fn::Sub"]))
        return result | set().union(*(references(x) for x in value.values()))

    graph = {name: (references(resource) | set(items(resource.get("DependsOn", [])))) & RESOURCES.keys()
             for name, resource in RESOURCES.items()}
    complete = set()

    def visit(name, active):
        assert name not in active, f"CloudFormation dependency cycle through {name}"
        if name not in complete:
            for dependency in graph[name]:
                visit(dependency, active | {name})
            complete.add(name)

    for name in graph:
        visit(name, set())


def test_login_style_is_bound_only_to_the_new_client_and_existing_domain_version():
    assert TEMPLATE["Parameters"]["CognitoLoginVersion"]["AllowedValues"] == ["1", "2"]
    for resource, condition, version in [("PreviewClassicLoginStyle", "UseClassicLogin", "1"),
                                         ("PreviewManagedLoginStyle", "UseManagedLogin", "2")]:
        assert RESOURCES[resource]["Condition"] == condition
        assert TEMPLATE["Conditions"][condition] == {"Fn::And": [
            {"Condition": "SupportedDeployment"}, {"Fn::Equals": [{"Ref": "CognitoLoginVersion"}, version]},
        ]}
        props = RESOURCES[resource]["Properties"]
        assert props["ClientId"] == {"Ref": "PreviewCognitoClient"}
        assert props["UserPoolId"] == {"Ref": "CognitoUserPoolId"}
        assert "ALL" not in json.dumps(props)
    assert RESOURCES["PreviewManagedLoginStyle"]["Properties"]["UseCognitoProvidedValues"] is True


def test_optional_existing_role_companion_grants_only_exact_code_pointer_metadata():
    resource = RESOURCES["StableViewerMetadataPolicy"]
    assert resource["Type"] == "AWS::IAM::ManagedPolicy"
    assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"
    assert resource["Condition"] == "IncludeStableMetadata"
    assert TEMPLATE["Parameters"]["IncludeStableCodeMetadataPolicy"]["Default"] == "true"
    assert TEMPLATE["Conditions"]["IncludeStableMetadata"] == {"Fn::And": [
        {"Condition": "SupportedDeployment"},
        {"Fn::Equals": [{"Ref": "IncludeStableCodeMetadataPolicy"}, "true"]},
    ]}
    props = resource["Properties"]
    assert props["Roles"] == ["clhear-github-deploy"]
    assert props["ManagedPolicyName"] == "clhear-viewer-code-metadata"
    statements = props["PolicyDocument"]["Statement"]
    assert len(statements) == 3 and all(s["Effect"] == "Allow" for s in statements)
    by_action = {s["Action"]: s for s in statements}
    assert set(by_action) == {"s3:GetObject", "s3:PutObject", "s3:ListBucket"}
    pointer = BUCKET_ARN + "/deployments/l1/viewer-release.json"
    assert by_action["s3:GetObject"]["Resource"] == by_action["s3:PutObject"]["Resource"] == pointer
    assert by_action["s3:ListBucket"]["Resource"] == BUCKET_ARN
    for action, statement in by_action.items():
        expected = {"aws:RequestedRegion": "us-east-1"}
        if action == "s3:PutObject":
            expected["s3:x-amz-server-side-encryption"] = "AES256"
        if action == "s3:ListBucket":
            expected["s3:prefix"] = "deployments/l1/viewer-release.json"
        assert statement["Condition"] == {"StringEquals": expected}
    assert len(json.dumps(props["PolicyDocument"], separators=(",", ":"))) <= 6144

"""
tools/aws_deploy.py  -  Part 3, Step 4: put the MCP server on AWS (ECS Express Mode, Stockholm), one checked
step at a time. Uses the AWS CLI you signed in with (`aws login`); no keys are stored anywhere.

Your account number is looked up at run time and never written into a file, because this repository is
public. The settings that are safe to publish live in deploy/.

Usage (from the candidate-rag folder, venv active, Docker Desktop running, `aws login` done):
    python tools/aws_deploy.py check      # read-only: who you are, Region, default network, what exists
    python tools/aws_deploy.py roles      # create the two IAM roles (free)
    python tools/aws_deploy.py repo       # create the ECR repository (pennies a month)
    python tools/aws_deploy.py push       # build from the current commit, push to ECR, show the scan
    python tools/aws_deploy.py create     # CREATE THE SERVICE: starts the ~US$1.33 a day charge
    python tools/aws_deploy.py update     # move the running service to the image for the current commit
    python tools/aws_deploy.py status     # read-only: state, URL, image, running tasks, /health
    python tools/aws_deploy.py logs       # Step 5: keep logs 14 days, prove no search text is logged, saved query
    python tools/aws_deploy.py delete     # delete the service: stops the charge (asks you to confirm)
    python tools/aws_deploy.py github     # Step 6: let GitHub Actions deploy, with no stored keys (free)
    python tools/aws_deploy.py endpoint   # print the service's URL, or nothing if none (for the pipeline)

Each command is safe to run again: it skips what already exists and says so.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEPLOY = PROJECT / "deploy"
REGION = "eu-north-1"  # fixed by the account type (AWS's new sign-up experience); see BUILD_LOG Step 0
REPO = "candidate-rag-mcp"
SERVICE = "candidate-rag-mcp"
CLUSTER = "default"
EXEC_ROLE = "ecsTaskExecutionRole"
INFRA_ROLE = "ecsInfrastructureRoleForExpressServices"
POLICIES = {
    EXEC_ROLE: ("trust-ecs-tasks.json", "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"),
    INFRA_ROLE: ("trust-ecs-infrastructure.json",
                 "arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices"),
}


# ---------------------------------------------------------------------------- helpers
def say(kind: str, msg: str) -> None:
    print(f"{kind:5} {msg}")


def aws(*args: str, check: bool = True, region: bool = True) -> dict | list | str | None:
    """Run an AWS CLI command and return its JSON output. On failure, print AWS's message and stop."""
    cmd = ["aws", *args, "--output", "json"] + (["--region", REGION] if region else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        if not check:
            return None
        say("FAIL", "aws " + " ".join(args[:2]))
        print("      " + r.stderr.strip().replace("\n", "\n      "))
        sys.exit(1)
    out = r.stdout.strip()
    return json.loads(out) if out.startswith(("{", "[")) else out


def run(cmd: list[str], input_text: str | None = None, env: dict | None = None) -> None:
    r = subprocess.run(cmd, input=input_text, text=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        say("FAIL", " ".join(cmd[:3]) + f" exited with {r.returncode}")
        sys.exit(1)


def account() -> str:
    return aws("sts", "get-caller-identity", region=False)["Account"]


def registry() -> str:
    return f"{account()}.dkr.ecr.{REGION}.amazonaws.com"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=PROJECT, capture_output=True, text=True).stdout.strip()


def role_arn(name: str) -> str | None:
    r = aws("iam", "get-role", "--role-name", name, check=False, region=False)
    return r["Role"]["Arn"] if r else None


def service_state() -> str:
    """ACTIVE, DRAINING, INACTIVE or MISSING, asked by name. list-services stops showing a service as soon as
    its deletion starts, but the name can't be reused until it has finished draining: on 21/10 `delete`
    reported it gone and the next `create` failed with "Unable to Start a service that is still Draining"."""
    r = aws("ecs", "describe-services", "--cluster", CLUSTER, "--services", SERVICE, check=False)
    services = (r or {}).get("services") or []
    return services[0].get("status", "MISSING") if services else "MISSING"


def find_service() -> dict | None:
    """The Express service's description, or None if it doesn't exist (or is already deleted)."""
    listing = aws("ecs", "list-services", "--cluster", CLUSTER, check=False)
    for arn in (listing or {}).get("serviceArns", []):
        if arn.rstrip("/").split("/")[-1] == SERVICE:
            d = aws("ecs", "describe-express-gateway-service", "--service-arn", arn, check=False)
            if d and d["service"]["status"]["statusCode"] != "INACTIVE":
                return d["service"]
    return None


def endpoint_of(svc: dict) -> str | None:
    for cfg in svc.get("activeConfigurations", []):
        for p in cfg.get("ingressPaths", []):
            if p.get("accessType") == "PUBLIC" and p.get("endpoint"):
                e = p["endpoint"]
                return e if e.startswith("http") else f"https://{e}"
    return None


def health(url: str) -> str:
    try:
        import httpx
        r = httpx.get(f"{url.rstrip('/')}/health", timeout=10)
        return f"{r.status_code} {r.text[:60]}"
    except Exception as e:  # not up yet, DNS not propagated, certificate not issued yet
        return type(e).__name__


# ---------------------------------------------------------------------------- commands
def cmd_check() -> None:
    ident = aws("sts", "get-caller-identity", region=False)
    say("OK", f"signed in as {ident['Arn'].split(':', 5)[-1]} (account number kept off screen logs)")
    say("OK", f"Region for every command: {REGION}")
    vpcs = aws("ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true")["Vpcs"]
    if not vpcs:
        say("FAIL", "no default VPC in this Region; Express Mode needs one (or subnets given explicitly)")
    else:
        subnets = aws("ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpcs[0]['VpcId']}")["Subnets"]
        public = [s for s in subnets if s.get("MapPublicIpOnLaunch")]
        zones = sorted({s["AvailabilityZone"] for s in public})
        ok = len(zones) >= 2
        say("OK" if ok else "FAIL", f"default VPC has {len(public)} public subnets in {len(zones)} zones "
            f"({', '.join(zones)}); Express Mode needs at least 2 zones")
    for name in (*SERVICE_LINKED_ROLES, *POLICIES):
        say("INFO", f"role {name}: {'exists' if role_arn(name) else 'not created yet'}")
    repo = aws("ecr", "describe-repositories", "--repository-names", REPO, check=False)
    say("INFO", f"ECR repository {REPO}: {'exists' if repo else 'not created yet'}")
    svc = find_service()
    say("INFO", f"service {SERVICE}: {svc['status']['statusCode'] + ' (CHARGING)' if svc else 'none (no charge)'}")
    local = subprocess.run(["docker", "image", "inspect", "candidate-rag-mcp:local"], capture_output=True)
    say("INFO", f"local image candidate-rag-mcp:local: {'present' if local.returncode == 0 else 'missing'}")


# AWS's own roles for the three services Express Mode drives. In a new account each is created on first use,
# and Express Mode then used the load balancer and auto-scaling ones within a second of creating them, before
# IAM had propagated them (CloudTrail, 21/10, 13:42:47-56 UTC): CreateLoadBalancer "AccessDenied", auto scaling
# "Unable to assume IAM role", deployment rolled back. Creating them up front avoids that race. All free.
SERVICE_LINKED_ROLES = {
    "AWSServiceRoleForECS": "ecs.amazonaws.com",
    "AWSServiceRoleForElasticLoadBalancing": "elasticloadbalancing.amazonaws.com",
    "AWSServiceRoleForApplicationAutoScaling_ECSService": "ecs.application-autoscaling.amazonaws.com",
}


def cmd_roles() -> None:
    # The service-linked roles first (see SERVICE_LINKED_ROLES). The first `create` failed on the ECS one:
    # "Unable to assume the service linked role. Please verify that the ECS service linked role exists."
    for name, service in SERVICE_LINKED_ROLES.items():
        if role_arn(name):
            say("SKIP", f"{name} already exists")
        else:
            aws("iam", "create-service-linked-role", "--aws-service-name", service, region=False)
            say("OK", f"created {name}")
    for name, (trust, policy) in POLICIES.items():
        if role_arn(name):
            say("SKIP", f"{name} already exists")
        else:
            aws("iam", "create-role", "--role-name", name,
                "--assume-role-policy-document", f"file://{DEPLOY / trust}", region=False)
            say("OK", f"created {name} (trusted by {json.loads((DEPLOY / trust).read_text())['Statement'][0]['Principal']['Service']})")
        aws("iam", "attach-role-policy", "--role-name", name, "--policy-arn", policy, region=False)
        attached = aws("iam", "list-attached-role-policies", "--role-name", name, region=False)["AttachedPolicies"]
        names = [p["PolicyName"] for p in attached]
        say("OK" if policy.rsplit("/", 1)[-1] in names else "FAIL", f"{name} policies: {', '.join(names)}")
    say("INFO", "new roles can take about a minute to be usable; wait at least 2 minutes before `create` if any were just made")


def cmd_repo() -> None:
    if aws("ecr", "describe-repositories", "--repository-names", REPO, check=False):
        say("SKIP", f"repository {REPO} already exists")
    else:
        aws("ecr", "create-repository", "--repository-name", REPO, "--image-tag-mutability", "IMMUTABLE",
            "--image-scanning-configuration", "scanOnPush=true")
        say("OK", f"created repository {REPO}: tags immutable, scanned on push")
    aws("ecr", "put-lifecycle-policy", "--repository-name", REPO,
        "--lifecycle-policy-text", f"file://{DEPLOY / 'ecr-lifecycle.json'}")
    say("OK", "lifecycle policy: keep the newest 3 images")


def cmd_push() -> None:
    tag = git("rev-parse", "--short=12", "HEAD")
    image = f"{registry()}/{REPO}:{tag}"
    if aws("ecr", "describe-images", "--repository-name", REPO, "--image-ids", f"imageTag={tag}", check=False):
        say("SKIP", f"image for commit {tag} is already in ECR; checking its scan")
        report_scan(tag)
        return
    if git("status", "--porcelain", "--untracked-files=no"):
        say("FAIL", "uncommitted changes. Commit first, so the image's tag names the exact code inside it.")
        sys.exit(1)
    say("INFO", f"building from commit {tag}")
    # --provenance=false --sbom=false: by default Docker Desktop's builder wraps the image in an image index
    # with an attestation manifest ("exporting attestation manifest", "exporting manifest list"). The tag
    # then points at the index, which ECR's basic scan doesn't scan: on 21/10 the first push reported
    # "ScanNotFoundException ... Image scan does not exist". One plain image is scanned on push.
    run(["docker", "build", "--provenance=false", "--sbom=false",
         "-t", f"candidate-rag-mcp:{tag}", "-t", image, str(PROJECT)])
    # Log in and push with a throwaway Docker settings folder that has no credential store. On Windows,
    # Docker keeps logins in Windows Credential Manager, which holds about 2,500 characters, and ECR's token
    # for a temporary role session (what `aws login` gives) is longer: "error storing credentials ... The
    # stub received bad data" (21/10; aws/aws-cli issue 5636). The token lives only in this folder, only for
    # the push, and the folder is deleted afterwards. DOCKER_HOST keeps Docker Desktop's current engine,
    # which would otherwise be lost with the default settings folder.
    engine = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                            capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="candidate_rag_docker_") as cfg:
        # One (empty) entry for the registry, not an empty list: when the file has no logins at all, Docker
        # treats it as unconfigured and falls back to the platform's store, which on Windows is the same
        # Credential Manager (docker/cli: `if !configFile.ContainsAuth() { ... DetectDefaultStore ... }`).
        # Seen 21/10: the first version of this fix used {"auths": {}} and failed the same way.
        (Path(cfg) / "config.json").write_text(json.dumps({"auths": {registry(): {}}}), encoding="utf-8")
        env = dict(os.environ, DOCKER_CONFIG=cfg, **({"DOCKER_HOST": engine} if engine else {}))
        password = aws("ecr", "get-login-password")
        run(["docker", "login", "--username", "AWS", "--password-stdin", registry()], input_text=password, env=env)
        run(["docker", "push", image], env=env)
    say("OK", f"pushed {REPO}:{tag} (the temporary login was deleted with its folder)")
    report_scan(tag)


def report_scan(tag: str) -> None:
    """Waits for ECR's scan on push and prints the findings. Polls rather than using `aws ecr wait
    image-scan-complete`: that waiter gives up at once if the scan hasn't been registered yet, which on 21/10
    (second push, commit 1f3c324) reported "no scan found" seconds after a push that was scanned normally."""
    say("INFO", "waiting for the vulnerability scan (usually under a minute)")

    def scan_done():
        found = aws("ecr", "describe-image-scan-findings", "--repository-name", REPO, "--image-id", f"imageTag={tag}",
                    check=False)
        status = ((found or {}).get("imageScanStatus") or {}).get("status")
        return found if status in ("COMPLETE", "FAILED", "UNSUPPORTED_IMAGE") else None
    found = wait_until(scan_done, "waiting for the scan", minutes=5, every=10)
    status = ((found or {}).get("imageScanStatus") or {}).get("status")
    if status != "COMPLETE":  # the first version reported a scan that never ran as "none" (21/10)
        say("FAIL", f"scan did not complete (status: {status or 'no scan found after 5 minutes'}); "
                    "don't `create` or `update` until it does")
        sys.exit(1)
    counts = ((found or {}).get("imageScanFindings") or {}).get("findingSeverityCounts") or {}
    say("OK" if not counts.get("CRITICAL") else "WARN",
        f"scan COMPLETE; findings by severity: {counts or 'none found'}"
        + ("  (critical: review before `create`)" if counts.get("CRITICAL") else ""))


def wait_until(predicate, what: str, minutes: int = 15, every: int = 15):
    t0 = time.monotonic()
    while time.monotonic() - t0 < minutes * 60:
        result = predicate()
        if result:
            return result
        print(f"      ... {what} ({int(time.monotonic() - t0)} s)")
        time.sleep(every)
    return None


def cmd_create() -> None:
    if find_service():
        say("SKIP", f"service {SERVICE} already exists; use `status`")
        return
    if service_state() == "DRAINING":
        say("INFO", f"the previous {SERVICE} is still draining; waiting for it to finish (often 5 to 10 minutes)")
        if not wait_until(lambda: service_state() != "DRAINING", "waiting for the drain to finish", minutes=20, every=20):
            say("FAIL", "still draining after 20 minutes; run `create` again later")
            sys.exit(1)
        say("OK", "drain finished")
    tag = git("rev-parse", "--short=12", "HEAD")
    if not aws("ecr", "describe-images", "--repository-name", REPO, "--image-ids", f"imageTag={tag}", check=False):
        say("FAIL", f"no image for the current commit ({tag}) in ECR. Run `push` first.")
        sys.exit(1)
    arns = {name: role_arn(name) for name in POLICIES}
    if not all(arns.values()):
        say("FAIL", "roles missing. Run `roles` first.")
        sys.exit(1)
    spec = json.loads((DEPLOY / "express-service.json").read_text())
    spec["executionRoleArn"] = arns[EXEC_ROLE]
    spec["infrastructureRoleArn"] = arns[INFRA_ROLE]
    spec["primaryContainer"]["image"] = f"{registry()}/{REPO}:{tag}"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(spec, f)  # holds the account number, so it lives in %TEMP% and is deleted straight after
    try:
        created = aws("ecs", "create-express-gateway-service", "--cli-input-json", f"file://{f.name}")
    finally:
        os.unlink(f.name)
    say("OK", f"service {SERVICE} created from image {tag}: charging from now (~US$1.33 a day)")
    svc = wait_until(lambda: (s := find_service()) and endpoint_of(s) and s, "waiting for the URL")
    url = endpoint_of(svc) if svc else None
    if not url:
        say("FAIL", "no public URL after 15 minutes; run `status` and paste it")
        sys.exit(1)
    say("OK", f"URL: {url}")
    set_allowed_host(svc, url)


def set_allowed_host(svc: dict, url: str) -> None:
    """The service's hostname is only known once it exists, so it's added to MCP_ALLOWED_HOSTS by an
    update. Until then /health works and /mcp answers 421, which is the host check doing its job."""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    current = svc["activeConfigurations"][0]["primaryContainer"]
    env = {e["name"]: e["value"] for e in current.get("environment") or []}
    if env.get("MCP_ALLOWED_HOSTS") == host:
        say("SKIP", f"MCP_ALLOWED_HOSTS already {host}")
    else:
        # Send the current container settings back unchanged (image, port, log configuration) with only
        # the environment changed, so the update can't reset anything else to a default.
        container = {k: v for k, v in current.items() if v not in (None, [], {})}
        container["environment"] = [e for e in current.get("environment") or [] if e["name"] != "MCP_ALLOWED_HOSTS"] \
            + [{"name": "MCP_ALLOWED_HOSTS", "value": host}]
        aws("ecs", "update-express-gateway-service", "--service-arn", svc["serviceArn"],
            "--primary-container", json.dumps(container))
        say("OK", f"MCP_ALLOWED_HOSTS set to {host}; a rolling deployment replaces the task (a few minutes)")
    ok = wait_until(lambda: health(url).startswith("200") and mcp_answers(url), "waiting for /health and /mcp", minutes=20)
    say("OK" if ok else "FAIL", f"{url}/health: {health(url)}; /mcp with the right host: {'200' if ok else 'not yet'}")


def cmd_update() -> None:
    """Roll the running service onto the image built from the current commit (`push` first). Same charge as
    before: one task at a time, the new one replacing the old once it is healthy."""
    svc = find_service()
    if not svc:
        say("FAIL", f"service {SERVICE}: none running. Run `create` instead.")
        sys.exit(1)
    tag = git("rev-parse", "--short=12", "HEAD")
    if not aws("ecr", "describe-images", "--repository-name", REPO, "--image-ids", f"imageTag={tag}", check=False):
        say("FAIL", f"no image for the current commit ({tag}) in ECR. Run `push` first.")
        sys.exit(1)
    url = endpoint_of(svc)
    current = svc["activeConfigurations"][0]["primaryContainer"]
    if current.get("image", "").endswith(f":{tag}"):
        say("SKIP", f"the service already runs image {tag}")
    else:
        # As in set_allowed_host: send the current container settings back with only the image changed.
        container = {k: v for k, v in current.items() if v not in (None, [], {})}
        container["image"] = f"{registry()}/{REPO}:{tag}"
        aws("ecs", "update-express-gateway-service", "--service-arn", svc["serviceArn"],
            "--primary-container", json.dumps(container))
        say("OK", f"update to image {tag} requested; the new task replaces the old once healthy (often 5 to 10 minutes)")

    def rolled_out() -> bool:
        s = (aws("ecs", "describe-services", "--cluster", CLUSTER, "--services", svc["serviceArn"], check=False)
             or {}).get("services", [{}])[0]
        primary = [d for d in s.get("deployments", []) if d.get("status") == "PRIMARY"]
        if len(s.get("deployments", [])) != 1 or not primary or primary[0].get("rolloutState") != "COMPLETED":
            return False
        td = aws("ecs", "describe-task-definition", "--task-definition", primary[0]["taskDefinition"], check=False)
        image = ((td or {}).get("taskDefinition", {}).get("containerDefinitions") or [{}])[0].get("image", "")
        return image.endswith(f":{tag}") and s.get("runningCount") == 1 and health(url).startswith("200") and mcp_answers(url)
    ok = wait_until(rolled_out, f"waiting for image {tag} to be the only one running and healthy", minutes=20, every=20)
    say("OK" if ok else "FAIL", f"running image {tag}, 1 task, /health {health(url)}" if ok
        else "not rolled out after 20 minutes; run `status` and paste it with the service events")


def mcp_answers(url: str) -> bool:
    try:
        import httpx
        r = httpx.post(f"{url.rstrip('/')}/mcp", timeout=20,
                       headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                           "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "aws_deploy", "version": "0"}}})
        return r.status_code == 200
    except Exception:
        return False


def cmd_status() -> None:
    svc = find_service()
    if not svc:
        say("INFO", f"service {SERVICE}: none (no charge)")
        return
    cfg = svc["activeConfigurations"][0] if svc.get("activeConfigurations") else {}
    pc = cfg.get("primaryContainer", {})
    url = endpoint_of(svc)
    ecs = aws("ecs", "describe-services", "--cluster", CLUSTER, "--services", svc["serviceArn"], check=False)
    s = (ecs or {}).get("services", [{}])[0]
    env = {e["name"]: e["value"] for e in pc.get("environment") or []}
    say("INFO", f"service {SERVICE}: {svc['status']['statusCode']} (CHARGING) {svc['status'].get('statusReason') or ''}")
    say("INFO", f"image tag: {pc.get('image', '?').rsplit(':', 1)[-1]}; cpu {cfg.get('cpu')}, memory {cfg.get('memory')}; "
                f"tasks min {cfg.get('scalingTarget', {}).get('minTaskCount')} max {cfg.get('scalingTarget', {}).get('maxTaskCount')}")
    say("INFO", f"running tasks {s.get('runningCount')} of desired {s.get('desiredCount')}; deployments in progress: "
                f"{sum(1 for d in s.get('deployments', []) if d.get('rolloutState') == 'IN_PROGRESS')}")
    say("INFO", f"URL: {url}; MCP_ALLOWED_HOSTS: {env.get('MCP_ALLOWED_HOSTS', '(not set)')}")
    if url:
        say("INFO", f"/health: {health(url)}; /mcp answers: {mcp_answers(url)}")


LOG_PREFIX = f"/aws/ecs/{CLUSTER}/{SERVICE}"  # Express Mode names the group after the service, plus a suffix
RETENTION_DAYS = 14  # the default is to keep logs forever, which is a slow, silent cost
QUERY_NAME = f"{SERVICE}/activity"
# Everything except the load balancer's health checks (about 4 a minute, all "GET /health ... 200"), newest first.
ACTIVITY_QUERY = ('fields @timestamp, @message\n| filter @message not like "GET /health"\n'
                  '| sort @timestamp desc\n| limit 200')
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}


def log_groups() -> list[dict]:
    r = aws("logs", "describe-log-groups", "--log-group-name-prefix", LOG_PREFIX, check=False)
    return (r or {}).get("logGroups", [])


def tool_call(url: str, name: str, arguments: dict) -> tuple[int, bool | None]:
    """(HTTP status, whether the tool reported an error) for one stateless MCP tool call."""
    import httpx
    r = httpx.post(f"{url.rstrip('/')}/mcp", timeout=60, headers=MCP_HEADERS,
                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    ok_json = r.headers.get("content-type", "").startswith("application/json")
    return r.status_code, (r.json().get("result", {}).get("isError") if ok_json else None)


def cmd_logs() -> None:
    """Step 5. Sets retention, then proves two things with a made-up marker word: that the server's logs reach
    CloudWatch, and that what someone searches for never does (only role, counts and filters are logged).
    Four calls carry the marker: a normal search, a query over the 500-character limit (a validation error,
    which repeats the input), an unknown role and an unknown candidate id (errors that name what was sent)."""
    svc = find_service()
    if not svc:
        say("FAIL", f"service {SERVICE}: none running. Run `create` first.")
        sys.exit(1)
    url = endpoint_of(svc)
    pc = (svc.get("activeConfigurations") or [{}])[0].get("primaryContainer", {})
    groups = log_groups()
    current = (pc.get("awsLogsConfiguration") or {}).get("logGroup") or \
        (max(groups, key=lambda g: g.get("creationTime", 0))["logGroupName"] if groups else None)
    if not current or current not in {g["logGroupName"] for g in groups}:
        say("FAIL", f"no log group found under {LOG_PREFIX}; run `status` and paste it")
        sys.exit(1)

    # 1. Retention, on this service's group and any left by an earlier service of the same name.
    for g in groups:
        name = g["logGroupName"]
        if g.get("retentionInDays") != RETENTION_DAYS:
            aws("logs", "put-retention-policy", "--log-group-name", name, "--retention-in-days", str(RETENTION_DAYS))
    for g in log_groups():
        label = "this service" if g["logGroupName"] == current else "an earlier service"
        say("OK" if g.get("retentionInDays") == RETENTION_DAYS else "FAIL",
            f"{g['logGroupName']} ({label}): kept {g.get('retentionInDays', 'forever')} days, "
            f"{g.get('storedBytes', 0) / 1e6:.2f} MB stored")

    # 2. The marker calls.
    marker = "canary" + secrets.token_hex(5)
    since_ms = int(time.time() * 1000) - 60_000
    calls = [
        ("a normal search", "search_candidates", {"role": "semi-senior", "query": f"{marker} credit control", "n_results": 5}, False),
        ("a query over 500 characters", "search_candidates", {"role": "semi-senior", "query": f"{marker} " + "x" * 600}, True),
        ("an unknown role", "search_candidates", {"role": marker, "query": "credit control"}, True),
        ("an unknown candidate id", "get_profile", {"candidate_id": marker}, True),
    ]
    for what, name, arguments, should_fail in calls:
        status, is_error = tool_call(url, name, arguments)
        ok = status == 200 and is_error is should_fail
        say("OK" if ok else "FAIL", f"{what}: HTTP {status}, " + ("refused, as it should be" if is_error else "answered")
            if ok else f"{what}: HTTP {status}, tool error {is_error} (expected {should_fail})")

    def events(pattern: str) -> list[dict]:
        r = aws("logs", "filter-log-events", "--log-group-name", current, "--start-time", str(since_ms),
                "--filter-pattern", pattern, check=False)
        return (r or {}).get("events", [])
    arrived = wait_until(lambda: events('"search_candidates role=semi-senior"'),
                         "waiting for the server's log line to reach CloudWatch", minutes=4, every=10)
    if not arrived:
        say("FAIL", "the normal search's log line hasn't reached CloudWatch after 4 minutes, so nothing below "
                    "can be trusted; paste this")
        sys.exit(1)
    say("OK", f"logs reach CloudWatch: {arrived[-1]['message'].strip()[-110:]}")
    time.sleep(15)  # let the error calls' lines (if any) arrive too
    leaked = events(f'"{marker}"')
    say("OK" if not leaked else "FAIL", "no log line contains the marker: what was searched for is not logged"
        if not leaked else f"{len(leaked)} log line(s) contain the marker (the text that was sent):")
    for e in leaked[:5]:
        print("      " + e["message"].strip()[:200])

    # 3. The saved query, then run it once over the last hour.
    found = aws("logs", "describe-query-definitions", "--query-definition-name-prefix", QUERY_NAME)
    existing = [q for q in found.get("queryDefinitions", []) if q.get("name") == QUERY_NAME]
    aws("logs", "put-query-definition", "--name", QUERY_NAME, "--query-string", ACTIVITY_QUERY,
        "--log-group-names", current, *(["--query-definition-id", existing[0]["queryDefinitionId"]] if existing else []))
    say("OK", f"saved query '{QUERY_NAME}' {'updated' if existing else 'created'} (CloudWatch > Logs Insights > Saved queries)")
    now = int(time.time())
    qid = aws("logs", "start-query", "--log-group-name", current, "--start-time", str(now - 3600),
              "--end-time", str(now), "--query-string", ACTIVITY_QUERY)["queryId"]
    res = wait_until(lambda: (r := aws("logs", "get-query-results", "--query-id", qid))["status"] in
                     ("Complete", "Failed", "Cancelled", "Timeout") and r, "running the saved query", minutes=2, every=3)
    if not res or res["status"] != "Complete":
        say("FAIL", f"saved query did not complete: {(res or {}).get('status', 'no answer after 2 minutes')}")
        sys.exit(1)
    rows = [{f["field"]: f["value"] for f in row} for row in res.get("results", [])]
    say("OK" if rows else "FAIL", f"saved query over the last hour: {len(rows)} lines that aren't health checks; newest:")
    for row in rows[:8]:
        print(f"      {row.get('@timestamp', '')[:19]}  {row.get('@message', '').strip()[:130]}")


GITHUB_REPO = "fabianaspey-droid/candidate-rag"
GITHUB_ENVIRONMENT = "production"  # the GitHub environment whose jobs may deploy; it has a required reviewer
OIDC_HOST = "token.actions.githubusercontent.com"
DEPLOY_ROLE = "candidate-rag-github-deploy"


def cmd_github() -> None:
    """Step 6: an IAM role that only GitHub Actions jobs in this repository's `production` environment can take,
    by OpenID Connect: GitHub signs a short-lived token for the job, AWS checks it, and the job gets credentials
    for under an hour. Nothing is stored in GitHub except the role's ARN. The role can push to this ECR
    repository and update services in Stockholm, nothing else (deploy/github-deploy-policy.json).

    The trust condition uses GitHub's newer subject format, with the owner's and repository's numeric IDs as well
    as their names (GitHub changelog, 23/04/2026: the default for repositories created after 15/07/2026; this one
    was created 14/10/2026). A deleted-and-recreated repository with the same name gets a new ID, so it can't
    take the role. The environment form below is inferred from GitHub's documented pattern, not quoted from it:
    the pipeline prints the subject it actually receives, so a mismatch shows up on the first run.

    Blocked on this account (21/10): "not authorized to perform: iam:ListOpenIDConnectProviders ... with an explicit
    deny in a service control policy". The Free plan account sits in an organisation AWS manages, whose policy
    denies GitHub-style identity providers; no permission in the account can override an explicit deny. The
    pipeline's deploy job stays off until this works (repository variable DEPLOY_ENABLED)."""
    import httpx
    r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}", timeout=20, headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        say("FAIL", f"GitHub API answered {r.status_code} for {GITHUB_REPO}")
        sys.exit(1)
    info = r.json()
    sub = (f"repo:{info['owner']['login']}@{info['owner']['id']}/{info['name']}@{info['id']}"
           f":environment:{GITHUB_ENVIRONMENT}")
    say("OK", f"{GITHUB_REPO}: repository id {info['id']}, owner id {info['owner']['id']}, created {info['created_at'][:10]}")
    acct = account()
    provider_arn = f"arn:aws:iam::{acct}:oidc-provider/{OIDC_HOST}"
    providers = aws("iam", "list-open-id-connect-providers", region=False)["OpenIDConnectProviderList"]
    if provider_arn in {p["Arn"] for p in providers}:
        say("SKIP", f"identity provider {OIDC_HOST} already exists")
    else:
        aws("iam", "create-open-id-connect-provider", "--url", f"https://{OIDC_HOST}",
            "--client-id-list", "sts.amazonaws.com", region=False)
        say("OK", f"created identity provider {OIDC_HOST} (audience sts.amazonaws.com)")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Federated": provider_arn}, "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {"StringEquals": {f"{OIDC_HOST}:aud": "sts.amazonaws.com", f"{OIDC_HOST}:sub": sub}}}]}
    policy = (DEPLOY / "github-deploy-policy.json").read_text(encoding="utf-8").replace("ACCOUNT_ID", acct)
    json.loads(policy)
    with tempfile.TemporaryDirectory(prefix="candidate_rag_iam_") as d:  # hold the account number; deleted after
        tp, pp = Path(d) / "trust.json", Path(d) / "policy.json"
        tp.write_text(json.dumps(trust), encoding="utf-8")
        pp.write_text(policy, encoding="utf-8")
        if role_arn(DEPLOY_ROLE):
            aws("iam", "update-assume-role-policy", "--role-name", DEPLOY_ROLE, "--policy-document", f"file://{tp}", region=False)
            say("OK", f"{DEPLOY_ROLE} exists; trust policy set again")
        else:
            aws("iam", "create-role", "--role-name", DEPLOY_ROLE, "--assume-role-policy-document", f"file://{tp}",
                "--description", f"GitHub Actions deploys for {GITHUB_REPO}, {GITHUB_ENVIRONMENT} environment only", region=False)
            say("OK", f"created {DEPLOY_ROLE}")
        aws("iam", "put-role-policy", "--role-name", DEPLOY_ROLE, "--policy-name", "deploy-candidate-rag-mcp",
            "--policy-document", f"file://{pp}", region=False)
    got = aws("iam", "get-role", "--role-name", DEPLOY_ROLE, region=False)["Role"]
    cond = got["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
    say("OK" if cond.get(f"{OIDC_HOST}:sub") == sub else "FAIL", f"{DEPLOY_ROLE} trusts only: {cond.get(f'{OIDC_HOST}:sub')}")
    perms = aws("iam", "get-role-policy", "--role-name", DEPLOY_ROLE, "--policy-name", "deploy-candidate-rag-mcp", region=False)
    actions = sorted({a for st in perms["PolicyDocument"]["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])})
    say("OK", f"{DEPLOY_ROLE} may: {', '.join(actions)}")
    print(f"\nIn GitHub, Settings > Environments > {GITHUB_ENVIRONMENT} > Environment secrets, add AWS_ROLE_ARN with this value:")
    print(f"  {got['Arn']}")


def cmd_endpoint() -> None:
    """The service's URL alone, or nothing if no service is running. The pipeline masks it before use."""
    svc = find_service()
    print((endpoint_of(svc) or "") if svc else "")


def cmd_delete() -> None:
    svc = find_service()
    if not svc:
        say("SKIP", f"service {SERVICE}: none to delete")
    else:
        answer = input(f"Type the service name ({SERVICE}) to delete it and stop the charge: ").strip()
        if answer != SERVICE:
            say("SKIP", "not deleted")
            return
        aws("ecs", "delete-express-gateway-service", "--service-arn", svc["serviceArn"])
        say("OK", "delete requested; waiting until it has fully drained (the charge stops as it goes)")
        gone = wait_until(lambda: service_state() in ("INACTIVE", "MISSING"), "waiting for the drain to finish",
                          minutes=20, every=20)
        say("OK" if gone else "WARN", "service fully deleted" if gone else "still draining after 20 minutes; `create` will wait for it")
    lbs = aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    say("OK" if not lbs else "WARN", f"load balancers left in {REGION}: {len(lbs)}"
        + ("" if not lbs else " (Express Mode removes its own when the last service goes; if one stays, tell me)"))


def main() -> int:
    commands = {"check": cmd_check, "roles": cmd_roles, "repo": cmd_repo, "push": cmd_push,
                "create": cmd_create, "update": cmd_update, "status": cmd_status, "logs": cmd_logs, "delete": cmd_delete,
                "github": cmd_github, "endpoint": cmd_endpoint}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(__doc__)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    commands[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())

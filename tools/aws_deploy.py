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
    python tools/aws_deploy.py status     # read-only: state, URL, image, running tasks, /health
    python tools/aws_deploy.py delete     # delete the service: stops the charge (asks you to confirm)

Each command is safe to run again: it skips what already exists and says so.
"""
from __future__ import annotations

import json
import os
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
    for name in POLICIES:
        say("INFO", f"role {name}: {'exists' if role_arn(name) else 'not created yet'}")
    repo = aws("ecr", "describe-repositories", "--repository-names", REPO, check=False)
    say("INFO", f"ECR repository {REPO}: {'exists' if repo else 'not created yet'}")
    svc = find_service()
    say("INFO", f"service {SERVICE}: {svc['status']['statusCode'] + ' (CHARGING)' if svc else 'none (no charge)'}")
    local = subprocess.run(["docker", "image", "inspect", "candidate-rag-mcp:local"], capture_output=True)
    say("INFO", f"local image candidate-rag-mcp:local: {'present' if local.returncode == 0 else 'missing'}")


def cmd_roles() -> None:
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
    say("INFO", "new roles can take about a minute to be usable; wait before `create` if you just made them")


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
    if git("status", "--porcelain", "--untracked-files=no"):
        say("FAIL", "uncommitted changes. Commit first, so the image's tag names the exact code inside it.")
        sys.exit(1)
    tag = git("rev-parse", "--short=12", "HEAD")
    image = f"{registry()}/{REPO}:{tag}"
    if aws("ecr", "describe-images", "--repository-name", REPO, "--image-ids", f"imageTag={tag}", check=False):
        say("SKIP", f"image for commit {tag} is already in ECR")
        return
    say("INFO", f"building from commit {tag}")
    run(["docker", "build", "-t", f"candidate-rag-mcp:{tag}", "-t", image, str(PROJECT)])
    # Log in and push with a throwaway Docker settings folder that has no credential store. On Windows,
    # Docker keeps logins in Windows Credential Manager, which holds about 2,500 characters, and ECR's token
    # for a temporary role session (what `aws login` gives) is longer: "error storing credentials ... The
    # stub received bad data" (21/10; aws/aws-cli issue 5636). The token lives only in this folder, only for
    # the push, and the folder is deleted afterwards. DOCKER_HOST keeps Docker Desktop's current engine,
    # which would otherwise be lost with the default settings folder.
    engine = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                            capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="candidate_rag_docker_") as cfg:
        (Path(cfg) / "config.json").write_text('{"auths": {}}', encoding="utf-8")
        env = dict(os.environ, DOCKER_CONFIG=cfg, **({"DOCKER_HOST": engine} if engine else {}))
        password = aws("ecr", "get-login-password")
        run(["docker", "login", "--username", "AWS", "--password-stdin", registry()], input_text=password, env=env)
        run(["docker", "push", image], env=env)
    say("OK", f"pushed {REPO}:{tag} (the temporary login was deleted with its folder)")
    say("INFO", "waiting for the vulnerability scan (usually under a minute)")
    aws("ecr", "wait", "image-scan-complete", "--repository-name", REPO, "--image-id", f"imageTag={tag}", check=False)
    found = aws("ecr", "describe-image-scan-findings", "--repository-name", REPO, "--image-id", f"imageTag={tag}",
                check=False)
    counts = ((found or {}).get("imageScanFindings") or {}).get("findingSeverityCounts") or {}
    say("OK" if not counts.get("CRITICAL") else "WARN",
        f"scan findings by severity: {counts or 'none'}" + ("  (critical: review before `create`)" if counts.get("CRITICAL") else ""))


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
        say("OK", "delete requested; waiting until it's gone")
        wait_until(lambda: find_service() is None, "waiting for the service to go", minutes=20)
    lbs = aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    say("OK" if not lbs else "WARN", f"load balancers left in {REGION}: {len(lbs)}"
        + ("" if not lbs else " (Express Mode removes its own when the last service goes; if one stays, tell me)"))


def main() -> int:
    commands = {"check": cmd_check, "roles": cmd_roles, "repo": cmd_repo, "push": cmd_push,
                "create": cmd_create, "status": cmd_status, "delete": cmd_delete}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(__doc__)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    commands[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())

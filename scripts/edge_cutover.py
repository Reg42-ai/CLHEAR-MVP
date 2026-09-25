"""Point clhear.reg42.ai at the CloudFront edge (infra/edge.tf) or back at the API.

    python scripts/edge_cutover.py --to edge|api

Only the A alias record moves. The new target must answer the public hostname
(health 200, an anonymous protected read 401) before the record changes; after
Route 53 reports the change in sync, the same checks run against the public
hostname, and a failed check puts the previous alias back.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

HOSTNAME = "clhear.reg42.ai"
REGION = "us-east-1"
CLOUDFRONT_ZONE = "Z2FDTNDATAQYW2"  # fixed hosted zone of every CloudFront distribution
CHECKS = (("/api/clhear/health", 200), ("/api/clhear/sources", 401))


def _clients():
    import boto3

    return {"route53": boto3.client("route53"), "cloudfront": boto3.client("cloudfront"),
            "apigatewayv2": boto3.client("apigatewayv2", region_name=REGION)}


def zone_id(clients, hostname: str = HOSTNAME) -> str:
    zone = hostname.split(".", 1)[1] + "."
    zones = [z for z in clients["route53"].list_hosted_zones_by_name(DNSName=zone)["HostedZones"]
             if z["Name"] == zone and not z["Config"].get("PrivateZone")]
    if len(zones) != 1:
        raise RuntimeError(f"Expected one public hosted zone named {zone}")
    return zones[0]["Id"].rsplit("/", 1)[-1]


def targets(clients, hostname: str = HOSTNAME) -> dict:
    """The two alias targets: the edge distribution and the API's regional domain."""
    distributions = [d for d in clients["cloudfront"].list_distributions()["DistributionList"].get("Items", [])
                     if hostname in (d.get("Aliases") or {}).get("Items", [])]
    if len(distributions) != 1 or distributions[0]["Status"] != "Deployed" or not distributions[0]["Enabled"]:
        raise RuntimeError(f"Expected one deployed, enabled distribution with the alias {hostname}")
    api = clients["apigatewayv2"].get_domain_name(DomainName=hostname)["DomainNameConfigurations"][0]
    return {"edge": {"DNSName": distributions[0]["DomainName"], "HostedZoneId": CLOUDFRONT_ZONE},
            "api": {"DNSName": api["ApiGatewayDomainName"], "HostedZoneId": api["HostedZoneId"]}}


def current_alias(clients, zone: str, hostname: str = HOSTNAME) -> dict | None:
    records = clients["route53"].list_resource_record_sets(HostedZoneId=zone, StartRecordName=hostname,
                                                           StartRecordType="A", MaxItems="1")["ResourceRecordSets"]
    if not records or records[0]["Name"].rstrip(".") != hostname or records[0]["Type"] != "A":
        return None
    alias = records[0].get("AliasTarget")
    return {"DNSName": alias["DNSName"].rstrip("."), "HostedZoneId": alias["HostedZoneId"]} if alias else None


def probe(connect_host: str, hostname: str = HOSTNAME) -> list[dict]:
    """Send the public hostname to ``connect_host`` and record each check's status."""
    results = []
    for path, expected in CHECKS:
        request = urllib.request.Request(f"https://{connect_host}{path}", headers={
            "Host": hostname, "Accept": "application/json", "User-Agent": "clhear-edge-cutover"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except Exception:  # noqa: BLE001 — a timeout or TLS failure is a failed check
            status = None
        results.append({"path": path, "expected": expected, "status": status,
                        "ms": round((time.monotonic() - started) * 1000)})
    return results


def _upsert(clients, zone: str, alias: dict, hostname: str = HOSTNAME) -> None:
    change = clients["route53"].change_resource_record_sets(HostedZoneId=zone, ChangeBatch={
        "Comment": "scripts/edge_cutover.py",
        "Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": hostname, "Type": "A",
            "AliasTarget": {"DNSName": alias["DNSName"], "HostedZoneId": alias["HostedZoneId"],
                            "EvaluateTargetHealth": False}}}]})["ChangeInfo"]
    clients["route53"].get_waiter("resource_record_sets_changed").wait(
        Id=change["Id"], WaiterConfig={"Delay": 10, "MaxAttempts": 60})


def cutover(clients, *, to: str, settle_s: int = 90, hostname: str = HOSTNAME) -> dict:
    zone = zone_id(clients, hostname)
    wanted = targets(clients, hostname)[to]
    previous = current_alias(clients, zone, hostname)
    report = {"to": to, "target": wanted["DNSName"], "previous": (previous or {}).get("DNSName"),
              "preflight": probe(wanted["DNSName"], hostname), "switched": False}
    if not all(r["status"] == r["expected"] for r in report["preflight"]):
        report["reason"] = "target does not answer the public hostname"
        return report
    if previous == wanted:
        report.update(switched=True, reason="already pointed at the target")
        return report
    _upsert(clients, zone, wanted, hostname)
    time.sleep(settle_s)
    report["after"] = probe(hostname, hostname)
    report["switched"] = all(r["status"] == r["expected"] for r in report["after"])
    if not report["switched"] and previous:
        _upsert(clients, zone, previous, hostname)
        report["reverted_to"] = previous["DNSName"]
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", choices=("edge", "api"), required=True)
    args = parser.parse_args(argv)
    result = cutover(_clients(), to=args.to)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["switched"] else 1


if __name__ == "__main__":
    sys.exit(main())

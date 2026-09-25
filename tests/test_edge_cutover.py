"""The public hostname moves to the edge only when the edge answers it, and moves back on a failed check."""
from scripts import edge_cutover

API = {"DNSName": "d-4tzjtbu0gh.execute-api.us-east-1.amazonaws.com", "HostedZoneId": "Z1UJRXOUMOOFQ8"}
EDGE = {"DNSName": "d1ncj7e3gwwhvj.cloudfront.net", "HostedZoneId": edge_cutover.CLOUDFRONT_ZONE}
GOOD = [{"path": p, "expected": s, "status": s, "ms": 1} for p, s in edge_cutover.CHECKS]
BAD = [{"path": p, "expected": s, "status": 403, "ms": 1} for p, s in edge_cutover.CHECKS]


class Waiter:
    def wait(self, **_):
        pass


class Route53:
    def __init__(self, alias):
        self.alias, self.changes = dict(alias), []

    def list_hosted_zones_by_name(self, DNSName):
        return {"HostedZones": [{"Id": "/hostedzone/ZREG42", "Name": DNSName, "Config": {"PrivateZone": False}}]}

    def list_resource_record_sets(self, **_):
        return {"ResourceRecordSets": [{"Name": "clhear.reg42.ai.", "Type": "A",
                                        "AliasTarget": {**self.alias, "DNSName": self.alias["DNSName"] + "."}}]}

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):
        change = ChangeBatch["Changes"][0]
        assert HostedZoneId == "ZREG42" and change["Action"] == "UPSERT" and change["ResourceRecordSet"]["Type"] == "A"
        target = change["ResourceRecordSet"]["AliasTarget"]
        self.alias = {"DNSName": target["DNSName"], "HostedZoneId": target["HostedZoneId"]}
        self.changes.append(self.alias["DNSName"])
        return {"ChangeInfo": {"Id": f"/change/{len(self.changes)}"}}

    def get_waiter(self, name):
        assert name == "resource_record_sets_changed"
        return Waiter()


class CloudFront:
    def list_distributions(self):
        return {"DistributionList": {"Items": [
            {"DomainName": "d3p4gvgw747nt5.cloudfront.net", "Aliases": {"Items": ["reg42.com"]}, "Status": "Deployed", "Enabled": True},
            {"DomainName": EDGE["DNSName"], "Aliases": {"Items": ["clhear.reg42.ai"]}, "Status": "Deployed", "Enabled": True}]}}


class ApiGateway:
    def get_domain_name(self, DomainName):
        return {"DomainNameConfigurations": [{"ApiGatewayDomainName": API["DNSName"], "HostedZoneId": API["HostedZoneId"]}]}


def _clients(alias=API):
    return {"route53": Route53(alias), "cloudfront": CloudFront(), "apigatewayv2": ApiGateway()}


def test_the_record_moves_to_the_edge_after_the_edge_answers_the_hostname(monkeypatch):
    probed = []
    monkeypatch.setattr(edge_cutover, "probe", lambda host, hostname=edge_cutover.HOSTNAME: probed.append(host) or GOOD)
    clients = _clients()
    report = edge_cutover.cutover(clients, to="edge", settle_s=0)
    assert report["switched"] and clients["route53"].changes == [EDGE["DNSName"]]
    assert probed == [EDGE["DNSName"], "clhear.reg42.ai"]


def test_an_edge_that_does_not_answer_leaves_the_record_alone(monkeypatch):
    monkeypatch.setattr(edge_cutover, "probe", lambda host, hostname=edge_cutover.HOSTNAME: BAD)
    clients = _clients()
    report = edge_cutover.cutover(clients, to="edge", settle_s=0)
    assert not report["switched"] and clients["route53"].changes == []


def test_a_failed_check_after_the_switch_restores_the_previous_alias(monkeypatch):
    answers = iter([GOOD, BAD])
    monkeypatch.setattr(edge_cutover, "probe", lambda host, hostname=edge_cutover.HOSTNAME: next(answers))
    clients = _clients()
    report = edge_cutover.cutover(clients, to="edge", settle_s=0)
    assert not report["switched"] and report["reverted_to"] == API["DNSName"]
    assert clients["route53"].changes == [EDGE["DNSName"], API["DNSName"]]


def test_moving_back_to_the_api_uses_its_regional_domain(monkeypatch):
    monkeypatch.setattr(edge_cutover, "probe", lambda host, hostname=edge_cutover.HOSTNAME: GOOD)
    clients = _clients(alias=EDGE)
    assert edge_cutover.cutover(clients, to="api", settle_s=0)["switched"]
    assert clients["route53"].alias == API

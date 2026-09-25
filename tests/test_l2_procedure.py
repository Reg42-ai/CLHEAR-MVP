"""Enforcement procedure, penalties and authority powers are not duties of the regulated person."""
import pytest

from app.clhear.l2.extract import detect_duty, not_a_duty

PROCEDURE = {
    "sec45(b)": "(b) Proceeding by Commission; modifying and setting aside orders\nWhenever the Commission shall have "
                "reason to believe that any such person, partnership, or corporation has been or is using any unfair "
                "method of competition or unfair or deceptive act or practice in or affecting commerce, and if it shall "
                "appear to the Commission that a proceeding by it in respect thereof would be to the interest of the "
                "public, it shall issue and serve upon such person, partnership, or corporation a complaint.",
    "sec45(c)": "(c) Review of order; rehearing\nAny person, partnership, or corporation required by an order of the "
                "Commission to cease and desist from using any method of competition or act or practice may obtain a "
                "review of such order in the court of appeals of the United States. A copy of such petition shall be "
                "forthwith transmitted by the clerk of the court to the Commission.",
    "sec45(f)": "(f) Service of complaints, orders and other processes; return\nComplaints, orders, and other processes "
                "of the Commission under this section may be served by anyone duly authorized by the Commission.",
    "sec45(l)": "(l) Penalty for violation of order; injunctions and other appropriate equitable relief\nAny person, "
                "partnership, or corporation who violates an order of the Commission after it has become final, and "
                "while such order is in effect, shall forfeit and pay to the United States a civil penalty of not more "
                "than $10,000 for each violation.",
    "sec45(m)": "(1)(A) The Commission may commence a civil action to recover a civil penalty in a district court of "
                "the United States against any person, partnership, or corporation which violates any rule.",
    "sec80b-3(k)(3)(A)": "(A) In general\nWhenever the Commission determines that the alleged violation specified in "
                         "the notice instituting proceedings is likely to result in significant dissipation of assets, "
                         "the Commission may enter a temporary order requiring the respondent to cease and desist. "
                         "Such temporary order shall become effective upon service upon the respondent.",
    "sec80b-3(c)": "(c) Procedure for registration; filing of application\n(1) An investment adviser may be registered "
                   "by filing with the Commission an application for registration. (2) Within forty-five days of the "
                   "date of the filing of such application the Commission shall by order grant such registration.",
}

DUTIES = {
    "sec80b-3(a)": "(a) Necessity of registration\nExcept as provided in subsection (b) and section 80b-3a of this "
                   "title, it shall be unlawful for any investment adviser, unless registered under this section, to "
                   "make use of the mails or any means or instrumentality of interstate commerce in connection with "
                   "his or its business as an investment adviser.",
    "sec80b-6": "It shall be unlawful for any investment adviser by use of the mails or any means or instrumentality "
                "of interstate commerce, directly or indirectly (1) to employ any device, scheme, or artifice to "
                "defraud any client or prospective client.",
    "248.30": "§ 248.30 Procedures to safeguard customer information, including response programs for unauthorized "
              "access to customer information and customer notice.\n(a) Every covered institution must develop, "
              "implement, and maintain written policies and procedures that address administrative, technical, and "
              "physical safeguards for the protection of customer information.",
    "255.5": "§ 255.5 Disclosure of material connections.\n(a) When there exists a connection between the endorser "
             "and the seller of the advertised product that might materially affect the weight or credibility of the "
             "endorsement, such connection must be disclosed clearly and conspicuously.",
}


@pytest.mark.parametrize("ref", sorted(PROCEDURE))
def test_enforcement_procedure_is_not_a_duty(ref):
    assert not_a_duty(PROCEDURE[ref], ref) and detect_duty(PROCEDURE[ref], ref) is None


@pytest.mark.parametrize("ref", sorted(DUTIES))
def test_duties_of_regulated_persons_stay_duties(ref):
    assert not not_a_duty(DUTIES[ref], ref) and detect_duty(DUTIES[ref], ref) is not None


def test_triage_never_asks_a_model_to_overrule_the_rules(engine):
    from app.clhear.l1.source_registry import seed
    from app.clhear.l2 import triage
    from tests.test_influencer_demo import _add_clauses

    seed(engine)
    _add_clauses(engine, "usc/15/ftc-act-45", {"sec45(c)": PROCEDURE["sec45(c)"], "sec45(m)": PROCEDURE["sec45(m)"]})
    candidates = triage._weak_candidates(engine)
    assert not [c for c in candidates if c["source_key"] == "usc/15/ftc-act-45"]

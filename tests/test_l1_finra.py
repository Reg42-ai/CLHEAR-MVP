"""FINRA regressions using original synthetic text in the publisher's DOM shape."""
import pytest

from app.clhear.l1.adapters.base import Artifact, DocNode, flatten
from app.clhear.l1.adapters.finra import normalize
from app.clhear.l1.adapters.sec_edgar import SecEdgarAdapter


def page(body: str, rule: str = "2210") -> bytes:
    return (
        f'<html><head><title>Unrelated browser title</title></head><body>'
        '<header>Cookie controls</header><article><p>Rulebook navigation</p></article>'
        f'<div id="the-rule"><h1>{rule}. Synthetic Rule</h1><div id="block-body">'
        f'<div class="field--name-body">{body}</div></div></div>'
        '<footer>Website footer</footer></body></html>'
    ).encode()


def adapter(rule: str = "2210", **kwargs) -> SecEdgarAdapter:
    return SecEdgarAdapter(
        channel="finra", source_key=f"finra/rule/{rule}", title=f"FINRA {rule}",
        url=kwargs.get("url", f"https://www.finra.org/rules-guidance/rulebooks/finra-rules/{rule}"),
    )


def artifacts(content: bytes) -> list[Artifact]:
    return [Artifact("page.html", content, "text/html")]


def source_text(tree) -> str:
    return normalize(" ".join(piece for node in flatten(tree) for piece in (node.label, node.heading, node.raw_text) if piece))


def provisions(tree):
    return {n.ref: n for n in flatten(tree) if n.node_type == "provision"}


def test_finra_selects_rule_field_and_preserves_div_only_paragraphs_once():
    content = page(
        '<div class="indent_firstpara"><strong>(a) Heading</strong></div>'
        '<div class="indent_firstpara"><p>Introductory words.</p>'
        '<div class="indent_secondpara">(1) First duty.</div>'
        '<div class="indent_secondpara">(2) Second duty.</div></div>'
    )
    a = adapter()
    tree = a.parse(content)
    assert a.validate_tree(tree, artifacts(content)) == []
    assert source_text(tree) == "2210. Synthetic Rule (a) Heading Introductory words. (1) First duty. (2) Second duty."
    assert set(provisions(tree)) == {"2210", "2210(a)", "2210(a)(1)", "2210(a)(2)"}
    assert provisions(tree)["2210(a)"].subtree_text() == "Heading\nIntroductory words."
    assert not any("visible" in n.ref or "fidelity" in n.ref for n in flatten(tree))


def test_nested_blocks_do_not_duplicate_children_and_keep_trailing_parent_text_in_order():
    content = page('<ul><li>(a) Parent duty.<ul><li>(1) Child duty.</li></ul>After the child.</li></ul>')
    a = adapter()
    tree = a.parse(content)
    assert a.validate_tree(tree, artifacts(content)) == []
    assert source_text(tree) == "2210. Synthetic Rule (a) Parent duty. (1) Child duty. After the child."
    assert source_text(tree).count("Child duty.") == 1


def test_deep_and_compound_numbering_returns_to_correct_parent():
    content = page(
        '<div class="indent_firstpara">(a) Scope.</div>'
        '<div class="indent_secondpara"><p>(1) First.</p>'
        '<div><p>(A) Detail.</p><div>(i) Roman first.</div><div>(ii) Roman second.</div></div>'
        '<div>(D)(i) Compound opening.<div>(ii) Compound continuation.</div></div></div>'
        '<div class="indent_secondpara">(2) Second.</div>'
        '<div class="indent_firstpara">(b) Next section.</div>'
    )
    a = adapter()
    tree = a.parse(content)
    assert a.validate_tree(tree, artifacts(content)) == []
    assert list(provisions(tree)) == [
        "2210", "2210(a)", "2210(a)(1)", "2210(a)(1)(A)",
        "2210(a)(1)(A)(i)", "2210(a)(1)(A)(ii)",
        "2210(a)(1)(D)(i)", "2210(a)(1)(D)(ii)", "2210(a)(2)", "2210(b)",
    ]


def test_roman_directly_below_letter_and_top_level_i_are_distinct():
    content = page(
        '<div class="indent_firstpara"><p>(f) Parent.</p>'
        '<div class="indent_secondpara">(i) First roman.</div>'
        '<div class="indent_secondpara">(ii) Second roman.</div></div>'
        '<div class="indent_firstpara"><p>(i) Top level letter.</p></div>', "3310",
    )
    a = adapter("3310")
    tree = a.parse(content)
    assert a.validate_tree(tree, artifacts(content)) == []
    assert list(provisions(tree)) == ["3310", "3310(f)", "3310(f)(i)", "3310(f)(ii)", "3310(i)"]


def test_supplementary_material_history_footnotes_and_inline_punctuation_are_retained():
    content = page(
        '<div>(a) Keep <em>this</em>, including records<sup>1</sup>.</div>'
        '<p>• • • Supplementary Material: • • •</p>'
        '<div>.01 First supplement.<div>(a) Supplement duty.</div></div>'
        '<div>.02 Second supplement.</div>'
        '<table class="footnote"><tr><td>1. Footnote text.<br/>Amended on a stated date.<br/>Prior history.</td></tr></table>'
    )
    a = adapter()
    tree = a.parse(content)
    assert a.validate_tree(tree, artifacts(content)) == []
    assert "Keep this, including records1." in source_text(tree)
    assert list(provisions(tree)) == ["2210", "2210(a)", "2210.01", "2210.01(a)", "2210.02"]
    assert source_text(tree).endswith("1. Footnote text. Amended on a stated date. Prior history.")
    assert "Footnote text" not in provisions(tree)["2210.02"].subtree_text()


def test_supplementary_date_parentheticals_are_not_paragraph_markers():
    content = page(
        '<div>.01 Delivery versus payment.</div>'
        '<div>(Date) First dated note.</div>'
        '<div>(Date) Second dated note.</div>'
        '<div>(a) Actual duty after the dates.</div>',
        "11630",
    )
    a = adapter("11630")
    tree = a.parse(content)
    assert a.validate_tree(tree, artifacts(content)) == []
    assert list(provisions(tree)) == ["11630", "11630.01", "11630.01(a)"]
    assert "First dated note." in provisions(tree)["11630.01"].subtree_text()
    assert "Second dated note." in provisions(tree)["11630.01"].subtree_text()


@pytest.mark.parametrize("corruption", ["drop_repeat", "duplicate", "reorder", "substring", "unparsed_marker"])
def test_strict_validator_rejects_text_and_clause_loss_even_when_membership_coverage_passes(corruption):
    content = page('<div>(a) Repeat duty.</div><div>(b) Repeat duty.</div><div>(c) A member must not act.</div>')
    a = adapter()
    tree = a.parse(content)
    if corruption == "drop_repeat":
        provisions(tree)["2210(b)"].raw_text = ""
    elif corruption == "duplicate":
        tree[0].children.append(DocNode(node_type="paragraph", raw_text="Repeat duty."))
    elif corruption == "reorder":
        tree[0].children[-1], tree[0].children[-2] = tree[0].children[-2], tree[0].children[-1]
    elif corruption == "substring":
        provisions(tree)["2210(c)"].raw_text = "A member must notice act."
    else:
        provisions(tree)["2210(b)"].node_type = "note"
    assert a.validate_tree(tree, artifacts(content))


def test_source_dom_ancestry_rejects_same_rule_wrong_parent_even_if_group_and_leaf_are_changed():
    content = page('<div>(a) Parent.<div>(1) Nested.</div></div>')
    a = adapter()
    tree = a.parse(content)
    for node in flatten(tree):
        if node.ref.startswith("2210(a)(1)"):
            node.ref = node.ref.replace("2210(a)(1)", "2210(b)(1)")
    failures = a.validate_tree(tree, artifacts(content))
    assert any("source DOM ancestor" in failure for failure in failures)


def test_source_rule_rejects_wrong_prefix_even_if_both_group_and_leaf_are_changed():
    content = page('<div>(a) Parent.</div>')
    a = adapter()
    tree = a.parse(content)
    for node in flatten(tree):
        if node.ref.startswith("2210(a)"):
            node.ref = node.ref.replace("2210", "3110")
    assert any("wrong source rule" in failure for failure in a.validate_tree(tree, artifacts(content)))


def test_source_marker_sequence_rejects_invented_intermediate_ancestor_in_flat_markup():
    content = page('<p>(a) Duty A.</p><p>(1) Detail one.</p>')
    a = adapter()
    tree = a.parse(content)
    for node in flatten(tree):
        if node.ref.startswith("2210(a)(1)"):
            node.ref = node.ref.replace("2210(a)(1)", "2210(a)(Z)(1)")
    assert any("unobserved source ancestor" in failure for failure in a.validate_tree(tree, artifacts(content)))


def test_source_identity_and_acquisition_url_must_match_article():
    content = page('<div>(a) Body.</div>', "3110")
    with pytest.raises(ValueError, match="source identity mismatch"):
        adapter("2210").parse(content)
    with pytest.raises(ValueError, match="acquisition URL"):
        adapter("3110", url="https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210").parse(content)
    valid_tree = adapter("3110").parse(content)
    assert adapter("2210").validate_tree(valid_tree, artifacts(content))


@pytest.mark.parametrize("content", [
    b'<html><body><div id="the-rule"><h1>2210. Title</h1><p>Navigation only.</p></div></body></html>',
    page(""),
    page("<div>(a) Body.</div>").replace(b"2210. Synthetic Rule", b"FINRA Rules"),
])
def test_missing_malformed_or_empty_rule_body_fails_closed(content):
    a = adapter()
    with pytest.raises(ValueError):
        a.parse(content)
    assert a.validate_tree([], artifacts(content))


def test_duplicate_paragraph_is_not_hidden_by_synthetic_ref_suffix():
    content = page('<div>(a) One.</div><div>(a) Duplicate.</div>')
    with pytest.raises(ValueError, match="duplicate paragraph reference"):
        adapter().parse(content)


def _heading_html(rule: str, title: str, children: list[str]) -> bytes:
    links = "".join(
        f'<li><a href="/rules-guidance/rulebooks/finra-rules/{child}">{child}</a></li>'
        for child in children
    )
    return (
        f'<html><body><div id="the-rule"><h1>{rule}. {title}</h1>'
        f'<div class="book-navigation"><ul>{links}</ul></div></div></body></html>'
    ).encode()


def test_series_heading_and_reserved_stub_are_navigation_pages():
    from app.clhear.l1.adapters.finra_document import NavigationPage

    reserved = _heading_html("1018", "Reserved", ["1017", "1000", "1019"])
    series = _heading_html("11300", "DELIVERY OF SECURITIES", ["11310", "11320", "11330"])
    with pytest.raises(NavigationPage, match="series heading or reserved stub"):
        adapter("1018").parse(reserved)
    with pytest.raises(NavigationPage, match="series heading or reserved stub"):
        adapter("11300").parse(series)
    missing = (
        b'<html><body><header>chrome</header><article><div id="the-rule">'
        b'<h1>2210. Communications</h1></div></article></body></html>'
    )
    with pytest.raises(ValueError, match="missing its official rule body field"):
        adapter().parse(missing)


def test_legacy_minimal_finra_fixture_remains_supported():
    content = b'<html><body><h1>3110. Supervision</h1><p>(a) Heading</p><p>Body.</p><p>(1) Child.</p></body></html>'
    a = adapter("3110")
    tree = a.parse(content)
    assert a.validate_tree(tree, artifacts(content)) == []
    assert list(provisions(tree)) == ["3110", "3110(a)", "3110(a)(1)"]


def test_finra_default_metadata_uses_finra_rights_and_direct_publisher():
    from app.clhear.l1.rights import rights_for

    meta = adapter().meta()
    assert meta.rights_basis == "derived_only"
    assert meta.rights_ref == rights_for("finra").ref
    assert "17 U.S.C." not in meta.rights_ref
    assert meta.publisher == "FINRA (official finra.org rulebook)"


def test_sec_channel_retains_generic_parser_and_does_not_apply_finra_guard():
    a = SecEdgarAdapter(channel="sec", source_key="sec/example", title="SEC", url="https://www.sec.gov/example")
    content = '<html><body><h1>SEC</h1><p>§ 240.15c3-3 A member must retain records.</p></body></html>'.encode()
    tree = a.parse(content)
    assert "§ 240.15c3-3" in provisions(tree)
    assert a.validate_tree(tree, artifacts(content)) == []

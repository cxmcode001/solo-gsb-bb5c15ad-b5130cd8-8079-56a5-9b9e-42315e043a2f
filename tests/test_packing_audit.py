"""End-to-end tests for the /packing-audit endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_OK = "09501101530003"  # valid Mod-10 check digit
GTIN2 = "00000000000017"  # second valid GTIN


def packing(containers=None, target_unit=None):
    body = {"containers": containers or []}
    if target_unit is not None:
        body["target_unit"] = target_unit
    return client.post("/packing-audit", json=body)


def scan(gtin=GTIN_OK, serial="SN1", weight="3103002000", extra=""):
    """Element-string scan with 01/21 and one weight AI (default 2 kg)."""
    parts = [f"01{gtin}"]
    if extra:
        parts.append(extra)
    parts.append(f"21{serial}{GS}")
    parts.append(weight)
    return "".join(parts)


def container(container_id, items=None, parent_id=None, max_weight=None):
    body = {"container_id": container_id, "items": items or []}
    if parent_id is not None:
        body["parent_id"] = parent_id
    if max_weight is not None:
        body["max_weight"] = max_weight
    return body


def by_id(body):
    return {entry["container_id"]: entry for entry in body["containers"]}


# ---------------------------------------------------------------- happy path


def test_leaf_to_root_aggregation_in_request_order():
    resp = packing(
        [
            container("PALLET-1", max_weight=25, items=[scan(serial="SN2", weight="3202002513")]),
            container(
                "BOX-1",
                parent_id="PALLET-1",
                max_weight=10,
                items=[scan(serial="SN1"), scan(serial="SN3", weight="3103001500")],
            ),
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "kg"
    # Response keeps the request order even though BOX-1 is the child.
    assert [c["container_id"] for c in body["containers"]] == ["PALLET-1", "BOX-1"]

    pallet, box = body["containers"]
    assert pallet["container_index"] == 0
    assert pallet["parent_id"] is None
    assert pallet["direct_weight"] == 11.398776  # 25.13 lb in kg
    assert pallet["total_weight"] == 14.898776  # 11.3987762581 + 3.5
    assert pallet["remaining_capacity"] == 10.101224
    assert pallet["over_limit"] is False
    assert pallet["direct_item_count"] == 1
    assert pallet["total_item_count"] == 3

    assert box["container_index"] == 1
    assert box["parent_id"] == "PALLET-1"
    assert box["direct_weight"] == 3.5
    assert box["total_weight"] == 3.5
    assert box["remaining_capacity"] == 6.5
    assert box["over_limit"] is False
    assert box["direct_item_count"] == 2
    assert box["total_item_count"] == 2


def test_deep_nesting_accumulates_through_every_level():
    resp = packing(
        [
            container("ITEM-BOX", parent_id="CARTON", items=[scan(serial="SN1", weight="3103001000")]),
            container("CARTON", parent_id="PALLET", items=[scan(serial="SN2", weight="3103001000")]),
            container("PALLET", parent_id="TRUCK", items=[scan(serial="SN3", weight="3103001000")]),
            container("TRUCK"),
        ]
    )
    assert resp.status_code == 200
    containers = by_id(resp.json())
    assert containers["ITEM-BOX"]["total_weight"] == 1.0
    assert containers["CARTON"]["total_weight"] == 2.0
    assert containers["PALLET"]["total_weight"] == 3.0
    assert containers["TRUCK"]["total_weight"] == 3.0
    assert containers["TRUCK"]["direct_item_count"] == 0
    assert containers["TRUCK"]["total_item_count"] == 3


def test_multiple_roots_form_a_forest():
    resp = packing(
        [
            container("A", items=[scan(serial="SN1")]),
            container("B", items=[scan(serial="SN2", weight="3103003000")]),
            container("B-CHILD", parent_id="B", items=[scan(serial="SN3")]),
        ]
    )
    assert resp.status_code == 200
    containers = by_id(resp.json())
    assert containers["A"]["total_weight"] == 2.0
    assert containers["B"]["total_weight"] == 5.0
    assert containers["B-CHILD"]["total_weight"] == 2.0


def test_container_without_max_weight_has_null_capacity():
    resp = packing([container("BOX", items=[scan()])])
    assert resp.status_code == 200
    (box,) = resp.json()["containers"]
    assert box["max_weight"] is None
    assert box["remaining_capacity"] is None
    assert box["over_limit"] is False


def test_max_weight_is_echoed_at_six_places():
    resp = packing([container("BOX", max_weight=10, items=[scan()])])
    assert resp.status_code == 200
    (box,) = resp.json()["containers"]
    assert box["max_weight"] == 10.0
    assert '"max_weight":10.000000' in resp.text


def test_over_limit_is_flagged_and_capacity_goes_negative():
    resp = packing(
        [
            container("PALLET", max_weight=3, items=[scan(serial="SN9")]),
            container("BOX", parent_id="PALLET", max_weight=1, items=[scan(serial="SN1")]),
        ]
    )
    assert resp.status_code == 200  # the full audit is still returned
    containers = by_id(resp.json())
    box = containers["BOX"]
    assert box["total_weight"] == 2.0
    assert box["over_limit"] is True
    assert box["remaining_capacity"] == -1.0
    pallet = containers["PALLET"]
    assert pallet["total_weight"] == 4.0
    assert pallet["over_limit"] is True
    assert pallet["remaining_capacity"] == -1.0


def test_exactly_at_the_limit_is_not_over():
    resp = packing([container("BOX", max_weight=2, items=[scan()])])
    assert resp.status_code == 200
    (box,) = resp.json()["containers"]
    assert box["over_limit"] is False
    assert box["remaining_capacity"] == 0.0


def test_limit_is_compared_on_the_unrounded_total():
    # 25.13 lb = 11.3987762581 kg: over a limit that equals the rounded value.
    resp = packing(
        [container("BOX", max_weight=11.398776, items=[scan(weight="3202002513")])]
    )
    assert resp.status_code == 200
    (box,) = resp.json()["containers"]
    assert box["total_weight"] == 11.398776
    assert box["over_limit"] is True


def test_target_unit_lb():
    resp = packing(
        [container("BOX", max_weight=5, items=[scan(weight="3100000001")])],  # 1 kg
        target_unit="lb",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "lb"
    (box,) = body["containers"]
    assert box["total_weight"] == 2.204623
    assert box["remaining_capacity"] == 2.795377


def test_mixed_units_in_one_container():
    resp = packing(
        [
            container(
                "BOX",
                items=[scan(serial="SN1"), scan(serial="SN2", weight="3202002513")],
            )
        ]
    )
    assert resp.status_code == 200
    (box,) = resp.json()["containers"]
    assert box["direct_weight"] == 13.398776  # 2 kg + 25.13 lb


def test_digital_link_scans_are_accepted():
    resp = packing(
        [
            container(
                "BOX",
                items=["https://id.gs1.org/01/09501101530003/21/SN1?3103=002000"],
            )
        ]
    )
    assert resp.status_code == 200
    (box,) = resp.json()["containers"]
    assert box["direct_weight"] == 2.0
    assert box["direct_item_count"] == 1


def test_ai_10_and_17_are_allowed_but_ignored():
    resp = packing(
        [container("BOX", items=[scan(extra=f"1725013110B1{GS}")])],
    )
    assert resp.status_code == 200
    assert resp.json()["containers"][0]["direct_weight"] == 2.0


def test_empty_request_is_200():
    resp = packing()
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "containers": []}


def test_container_with_empty_items():
    resp = packing([container("EMPTY"), container("BOX", items=[scan()])])
    assert resp.status_code == 200
    containers = by_id(resp.json())
    assert containers["EMPTY"]["direct_weight"] == 0.0
    assert containers["EMPTY"]["total_item_count"] == 0


def test_response_shape():
    resp = packing([container("BOX", max_weight=5, items=[scan()])])
    body = resp.json()
    assert set(body) == {"target_unit", "containers"}
    assert set(body["containers"][0]) == {
        "container_index",
        "container_id",
        "parent_id",
        "max_weight",
        "direct_weight",
        "total_weight",
        "remaining_capacity",
        "over_limit",
        "direct_item_count",
        "total_item_count",
    }


def test_weights_render_as_exact_six_place_numbers():
    resp = packing([container("BOX", max_weight=5, items=[scan()])])
    assert resp.status_code == 200
    assert '"direct_weight":2.000000' in resp.text
    assert '"total_weight":2.000000' in resp.text
    assert '"remaining_capacity":3.000000' in resp.text
    assert '"direct_weight":"2.000000"' not in resp.text


def test_half_even_rounding_at_six_places():
    # 3107 000025 -> 0.0000025 kg -> half-even rounds down to 0.000002
    resp = packing([container("BOX", items=[scan(weight="3107000025")])])
    assert resp.status_code == 200
    assert resp.json()["containers"][0]["total_weight"] == 0.000002
    # 3107 000075 -> 0.0000075 kg -> half-even rounds up to 0.000008
    resp = packing([container("BOX", items=[scan(weight="3107000075")])])
    assert resp.status_code == 200
    assert resp.json()["containers"][0]["total_weight"] == 0.000008


# ------------------------------------------------------ structural failures


def test_duplicate_container_id_is_422():
    resp = packing([container("BOX"), container("BOX")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 1
    assert detail["first_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_CONTAINER"
    assert "BOX" in detail["error"]["message"]
    assert set(detail) == {"container_index", "first_index", "error"}


def test_unknown_parent_is_422():
    resp = packing([container("BOX", parent_id="GHOST")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["error"]["error"] == "UNKNOWN_PARENT"
    assert "GHOST" in detail["error"]["found"]
    assert set(detail) == {"container_index", "error"}


def test_container_parented_to_itself_is_a_cycle():
    resp = packing([container("BOX", parent_id="BOX")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["cycle"] == [0]
    assert detail["error"]["error"] == "CYCLE_DETECTED"


def test_two_container_cycle_is_422():
    resp = packing(
        [
            container("A", parent_id="B"),
            container("B", parent_id="A"),
            container("C"),  # untouched root
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["container_index"] == 0
    assert detail["cycle"] == [0, 1]


def test_longer_cycle_is_422():
    resp = packing(
        [
            container("ROOT"),
            container("A", parent_id="C"),
            container("B", parent_id="A"),
            container("C", parent_id="B"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["cycle"] == [1, 3, 2]


def test_duplicate_item_across_containers_is_422():
    resp = packing(
        [
            container("A", items=[scan(serial="SN1"), scan(serial="SN2")]),
            container("B", items=[scan(serial="SN3"), scan(serial="SN1")]),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 1
    assert detail["item_index"] == 1
    assert detail["first_container_index"] == 0
    assert detail["first_item_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_ITEM"
    assert "SN1" in detail["error"]["message"]
    assert set(detail) == {
        "container_index",
        "item_index",
        "first_container_index",
        "first_item_index",
        "error",
    }


def test_duplicate_item_within_one_container_is_422():
    resp = packing([container("A", items=[scan(serial="SN1"), scan(serial="SN1")])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 1
    assert detail["first_container_index"] == 0
    assert detail["first_item_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_ITEM"


def test_same_serial_on_different_gtins_is_allowed():
    resp = packing(
        [
            container(
                "A",
                items=[scan(serial="SN1"), scan(gtin=GTIN2, serial="SN1")],
            )
        ]
    )
    assert resp.status_code == 200
    assert resp.json()["containers"][0]["direct_item_count"] == 2


def test_structure_is_checked_before_scans_are_parsed():
    # The duplicate id is reported, not the unparsable scan.
    resp = packing(
        [container("A", items=["bad"]), container("A", items=[scan()])]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_CONTAINER"


# ----------------------------------------------------------- scan failures


def test_parse_failure_reports_container_and_item_index():
    resp = packing(
        [
            container("A", items=[scan()]),
            container("B", items=[scan(serial="SN2"), "bad"]),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 1
    assert detail["item_index"] == 1
    assert detail["error"]["error"] == "INVALID_AI"
    assert set(detail) == {"container_index", "item_index", "error"}


def test_invalid_check_digit_reports_location():
    resp = packing([container("A", items=["010950110153000421SN1"])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 0
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


def test_missing_serial_is_422():
    resp = packing([container("A", items=[f"01{GTIN_OK}3103002000"])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 21" in detail["error"]["message"]


def test_missing_weight_is_422():
    resp = packing([container("A", items=[f"01{GTIN_OK}21SN1"])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "MISSING_AI"
    assert "310n/320n" in detail["error"]["message"]


def test_duplicate_weight_is_422():
    resp = packing(
        [container("A", items=[f"01{GTIN_OK}21SN1{GS}31030020003103001000"])]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


def test_duplicate_serial_ai_is_422():
    resp = packing(
        [container("A", items=[f"01{GTIN_OK}21SN1{GS}21SN2{GS}3103002000"])]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_AI"


# --------------------------------------------------------- request failures


def test_missing_fields_are_422():
    assert client.post("/packing-audit", json={}).status_code == 422
    assert client.post("/packing-audit", json={"containers": [{}]}).status_code == 422
    assert (
        client.post(
            "/packing-audit", json={"containers": [{"container_id": "A"}]}
        ).status_code
        == 422
    )


def test_empty_container_id_is_422():
    resp = packing([container("", items=[scan()])])
    assert resp.status_code == 422


def test_negative_max_weight_is_422():
    resp = packing([container("A", max_weight=-1, items=[scan()])])
    assert resp.status_code == 422


def test_invalid_target_unit_is_422():
    resp = packing(target_unit="g")
    assert resp.status_code == 422

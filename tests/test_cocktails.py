"""Backend tests for Five Score Slider cocktail API."""
import os
import pytest
import requests

BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL', 'https://five-score-slider.preview.emergentagent.com').rstrip('/')


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---- List endpoint ----
class TestListCocktails:
    def test_list_returns_seeded(self, api):
        r = api.get(f"{BASE_URL}/api/cocktails", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 15, f"expected >=15 cocktails, got {len(data)}"
        c = data[0]
        for f in ["id", "name", "description", "image_url", "strong", "fancy", "comfort", "party", "thirsty"]:
            assert f in c, f"missing field {f}"
        # ensure no _id leaked
        assert "_id" not in c


# ---- Match endpoint valid combos ----
@pytest.mark.parametrize("combo", [
    (1, 1, 1, 1, 1),
    (10, 10, 10, 10, 10),
    (5, 5, 5, 5, 5),
    (7, 3, 8, 2, 4),
])
class TestMatchValid:
    def test_match_returns_cocktail(self, api, combo):
        strong, fancy, comfort, party, thirsty = combo
        params = {"strong": strong, "fancy": fancy, "comfort": comfort, "party": party, "thirsty": thirsty}
        r = api.get(f"{BASE_URL}/api/cocktails/match", params=params, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "cocktail" in data and "distance" in data and "query" in data
        c = data["cocktail"]
        assert c["name"] and c["description"] and c["image_url"]
        for f in ["strong", "fancy", "comfort", "party", "thirsty"]:
            assert 1 <= c[f] <= 10
        assert data["query"] == params
        assert data["distance"] >= 0


# ---- Match endpoint invalid ----
class TestMatchInvalid:
    @pytest.mark.parametrize("params", [
        {"strong": 0, "fancy": 5, "comfort": 5, "party": 5, "thirsty": 5},
        {"strong": 5, "fancy": 11, "comfort": 5, "party": 5, "thirsty": 5},
        {"strong": 5, "fancy": 5, "comfort": -1, "party": 5, "thirsty": 5},
        {"strong": 5, "fancy": 5, "comfort": 5, "party": 99, "thirsty": 5},
        {"strong": 5, "fancy": 5, "comfort": 5, "party": 5, "thirsty": 0},
    ])
    def test_out_of_range_returns_400(self, api, params):
        r = api.get(f"{BASE_URL}/api/cocktails/match", params=params, timeout=30)
        assert r.status_code == 400, r.text

    def test_missing_param_returns_422(self, api):
        # FastAPI validation error for missing required query params
        r = api.get(f"{BASE_URL}/api/cocktails/match", params={"strong": 5}, timeout=30)
        assert r.status_code == 422


# ---- Nearest neighbor correctness ----
class TestNearestNeighbor:
    def test_extreme_matches_expected_profile(self, api):
        # (10,3,4,10,6) is exactly Long Island Iced Tea
        params = {"strong": 10, "fancy": 3, "comfort": 4, "party": 10, "thirsty": 6}
        r = api.get(f"{BASE_URL}/api/cocktails/match", params=params, timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert data["distance"] == 0.0
        assert data["cocktail"]["name"] == "Long Island Iced Tea"

    def test_low_strong_high_thirsty_returns_light_drink(self, api):
        # (1,2,6,3,10) is Lemonade
        params = {"strong": 1, "fancy": 2, "comfort": 6, "party": 3, "thirsty": 10}
        r = api.get(f"{BASE_URL}/api/cocktails/match", params=params, timeout=30)
        assert r.status_code == 200
        assert r.json()["cocktail"]["name"] == "Lemonade"

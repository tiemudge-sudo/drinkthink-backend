import asyncio
import pytest

from location_geocoding import GeocodingError, geocode_canonical_address, validate_geocodio_result

ADDRESS = {"line1": "3417 S. Almeria Ave", "line2": None, "city": "Tampa", "state": "FL", "postal_code": "33629", "country": "US"}
RESULT = {"location": {"lat": 27.0, "lng": -82.0}, "accuracy": 1, "accuracy_type": "rooftop", "address_components": {"number": "3417", "city": "Tampa", "state": "FL", "zip": "33629", "country": "US"}}


def test_accepts_address_level_result_in_geojson_longitude_latitude_order():
    assert validate_geocodio_result(RESULT, ADDRESS) == {"type": "Point", "coordinates": [-82.0, 27.0]}


@pytest.mark.parametrize("result", [{}, {**RESULT, "location": {"lat": "bad", "lng": -82}}, {**RESULT, "accuracy_type": "place"}, {**RESULT, "accuracy": 0.5}])
def test_rejects_no_result_malformed_coordinates_and_insufficient_accuracy(result):
    with pytest.raises(GeocodingError):
        validate_geocodio_result(result, ADDRESS)


class MockResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload


class MockGeocodioClient:
    def __init__(self, payload): self.payload, self.calls = payload, 0
    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return MockResponse(self.payload)


def test_mocked_geocodio_success_and_no_results():
    client = MockGeocodioClient({"results": [RESULT]})
    assert asyncio.run(geocode_canonical_address(ADDRESS, "test-key", client)) == {"type": "Point", "coordinates": [-82.0, 27.0]}
    assert client.calls == 1
    with pytest.raises(GeocodingError, match="no results"):
        asyncio.run(geocode_canonical_address(ADDRESS, "test-key", MockGeocodioClient({"results": []})))

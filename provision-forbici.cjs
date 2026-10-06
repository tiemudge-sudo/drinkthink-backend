const body = {
  location_id: "loc_forbici_south_tampa",
  organization_id: "org_forbici_modern_italian",
  name: "Forbici South Tampa",
  address: {
    line1: "1633 W Snow Ave",
    line2: null,
    city: "Tampa",
    state: "FL",
    postal_code: "33606",
    country: "US"
  },
  timezone: "America/New_York",
  status: "active"
};

(async () => {
  const response = await fetch(
    "https://drinkthink-backend-development.up.railway.app/api/admin/locations",
    {
      method: "POST",
      headers: {
        Authorization: "Bearer " + process.env.DRINKTHINK_LOCATION_PROVISIONING_API_KEY,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    }
  );

  console.log("HTTP", response.status);
  console.log(await response.text());
  if (!response.ok) process.exit(1);
})();

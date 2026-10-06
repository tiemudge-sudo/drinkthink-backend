const url =
  "https://drinkthink-backend-development.up.railway.app/api/admin/locations/loc_ties_house/rebuild-drinks";

(async () => {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization:
        `Bearer ${process.env.DRINKTHINK_LOCATION_INVENTORY_REBUILD_API_KEY}`,
    },
  });

  console.log("HTTP", response.status);
  console.log(await response.text());
  process.exit(response.ok ? 0 : 1);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
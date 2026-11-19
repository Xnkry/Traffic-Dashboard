from pathlib import Path

import pandas as pd


CITY_POINTS = [
    ("Casablanca", "Casablanca-Settat", [("Centre Ville", 33.5731, -7.5898), ("Ain Diab", 33.5928, -7.6511), ("Sidi Bernoussi", 33.6086, -7.5013)]),
    ("Rabat", "Rabat-Salé-Kénitra", [("Hassan", 34.0209, -6.8416), ("Agdal", 33.9975, -6.8478), ("Hay Riad", 33.9576, -6.8694)]),
    ("Marrakech", "Marrakech-Safi", [("Guéliz", 31.6340, -8.0107), ("Hivernage", 31.6195, -8.0085), ("Palmeraie", 31.6700, -7.9730)]),
    ("Tangier", "Tanger-Tétouan-Al Hoceïma", [("Corniche", 35.7750, -5.7910), ("Malabata", 35.7770, -5.7640), ("Iberia", 35.7771, -5.8230)]),
    ("Fès", "Fès-Meknès", [("Ville Nouvelle", 34.0340, -5.0020), ("Fès Jdid", 34.0550, -4.9910), ("Saïss", 33.9930, -5.0130)]),
    ("Agadir", "Souss-Massa", [("Talborjt", 30.4250, -9.5920), ("Founty", 30.3950, -9.5900), ("Dakhla district", 30.4050, -9.5550)]),
    ("Meknès", "Fès-Meknès", [("Hamria", 33.9000, -5.5400), ("Place Lahdim", 33.8935, -5.5650), ("Marjane", 33.8610, -5.5500)]),
    ("Oujda", "Oriental", [("Centre", 34.6810, -1.9080), ("Al Qods", 34.6620, -1.9110), ("Lazaret", 34.7020, -1.9130)]),
    ("Kénitra", "Rabat-Salé-Kénitra", [("Maamora", 34.2600, -6.5740), ("Centre", 34.2641, -6.5830), ("Bir Rami", 34.2360, -6.5780)]),
    ("Tétouan", "Tanger-Tétouan-Al Hoceïma", [("Centre", 35.5700, -5.3700), ("Wilaya", 35.5840, -5.3500), ("Touilaa", 35.5580, -5.3870)]),
    ("El Jadida", "Casablanca-Settat", [("Corniche", 33.2460, -8.4960), ("Centre", 33.2390, -8.5000), ("Sidi Bouzid", 33.2290, -8.5500)]),
    ("Safi", "Marrakech-Safi", [("Centre", 32.2990, -9.2370), ("Plateau", 32.2850, -9.2300), ("Sidi Bouzid", 32.3260, -9.2530)]),
]


def catalog() -> pd.DataFrame:
    places = pd.read_csv(Path(__file__).parent / "data/cities.csv")
    original = {city: points for city, _, points in CITY_POINTS}
    rows = []
    for city in places.itertuples():
        points = original.get(city.city, [("City centre", city.lat, city.lon)])
        for name, lat, lon in points:
            rows.append({"location_key": f"{city.city}/{name}", "city": city.city,
                "region": city.region, "location": name, "lat": lat, "lon": lon,
                "geoname_id": city.geoname_id, "population": city.population,
                "coordinate_source": "Original district catalog" if city.city in original else "GeoNames"})
    return pd.DataFrame(rows)

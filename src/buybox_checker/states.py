"""One delivery zipcode per US state, used to check the Amazon buybox state by state.

Each zipcode is a normal street-delivery zipcode of a big city in that state (not PO-box-only).
Zipcodes are strings so leading zeros (e.g. "06103") are kept. Latitude/longitude are the
approximate city center, only used to place the zipcode's dot on the web UI's map.
On first use, the checker logs the city Amazon accepted for each zipcode, or fails if Amazon rejects it.

GMV_TIERS groups the states by their rough share of US Amazon GMV, largest first: core markets (about 40%) and
growth markets (about 25%); the other 38 states are the long tail (about 35%).
PRESETS are ready-made selections, "core" and "core-growth", so a check can cover most sales with fewer states.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class State:
    """A US state and the zipcode used to check it."""

    code: str
    name: str
    zipcode: str
    city: str
    lat: float
    lon: float


STATES: dict[str, State] = {
    state.code: state
    for state in [
        State("AL", "Alabama", "35203", "Birmingham", 33.52, -86.81),
        State("AK", "Alaska", "99501", "Anchorage", 61.22, -149.90),
        State("AZ", "Arizona", "85004", "Phoenix", 33.45, -112.07),
        State("AR", "Arkansas", "72201", "Little Rock", 34.75, -92.29),
        State("CA", "California", "90012", "Los Angeles", 34.06, -118.24),
        State("CO", "Colorado", "80202", "Denver", 39.75, -104.99),
        State("CT", "Connecticut", "06103", "Hartford", 41.76, -72.67),
        State("DE", "Delaware", "19801", "Wilmington", 39.74, -75.55),
        State("FL", "Florida", "33131", "Miami", 25.77, -80.19),
        State("GA", "Georgia", "30303", "Atlanta", 33.75, -84.39),
        State("HI", "Hawaii", "96813", "Honolulu", 21.31, -157.86),
        State("ID", "Idaho", "83702", "Boise", 43.62, -116.20),
        State("IL", "Illinois", "60601", "Chicago", 41.89, -87.62),
        State("IN", "Indiana", "46204", "Indianapolis", 39.77, -86.16),
        State("IA", "Iowa", "50309", "Des Moines", 41.59, -93.62),
        State("KS", "Kansas", "67202", "Wichita", 37.69, -97.34),
        State("KY", "Kentucky", "40202", "Louisville", 38.25, -85.76),
        State("LA", "Louisiana", "70112", "New Orleans", 29.95, -90.07),
        State("ME", "Maine", "04101", "Portland", 43.66, -70.26),
        State("MD", "Maryland", "21201", "Baltimore", 39.29, -76.61),
        State("MA", "Massachusetts", "02108", "Boston", 42.36, -71.06),
        State("MI", "Michigan", "48226", "Detroit", 42.33, -83.05),
        State("MN", "Minnesota", "55401", "Minneapolis", 44.98, -93.27),
        State("MS", "Mississippi", "39201", "Jackson", 32.30, -90.18),
        State("MO", "Missouri", "64105", "Kansas City", 39.10, -94.58),
        State("MT", "Montana", "59101", "Billings", 45.78, -108.50),
        State("NE", "Nebraska", "68102", "Omaha", 41.26, -95.94),
        State("NV", "Nevada", "89101", "Las Vegas", 36.17, -115.14),
        State("NH", "New Hampshire", "03101", "Manchester", 42.99, -71.46),
        State("NJ", "New Jersey", "07102", "Newark", 40.74, -74.17),
        State("NM", "New Mexico", "87102", "Albuquerque", 35.08, -106.65),
        State("NY", "New York", "10001", "New York", 40.75, -73.99),
        State("NC", "North Carolina", "28202", "Charlotte", 35.23, -80.84),
        State("ND", "North Dakota", "58102", "Fargo", 46.88, -96.79),
        State("OH", "Ohio", "43215", "Columbus", 39.96, -83.00),
        State("OK", "Oklahoma", "73102", "Oklahoma City", 35.47, -97.52),
        State("OR", "Oregon", "97204", "Portland", 45.52, -122.68),
        State("PA", "Pennsylvania", "19103", "Philadelphia", 39.95, -75.17),
        State("RI", "Rhode Island", "02903", "Providence", 41.82, -71.41),
        State("SC", "South Carolina", "29201", "Columbia", 34.00, -81.03),
        State("SD", "South Dakota", "57104", "Sioux Falls", 43.54, -96.73),
        State("TN", "Tennessee", "37203", "Nashville", 36.16, -86.78),
        State("TX", "Texas", "77002", "Houston", 29.76, -95.37),
        State("UT", "Utah", "84101", "Salt Lake City", 40.76, -111.89),
        State("VT", "Vermont", "05401", "Burlington", 44.48, -73.21),
        State("VA", "Virginia", "23219", "Richmond", 37.54, -77.44),
        State("WA", "Washington", "98101", "Seattle", 47.61, -122.33),
        State("WV", "West Virginia", "25301", "Charleston", 38.35, -81.63),
        State("WI", "Wisconsin", "53202", "Milwaukee", 43.04, -87.91),
        State("WY", "Wyoming", "82001", "Cheyenne", 41.14, -104.82),
    ]
}


@dataclass(frozen=True)
class Tier:
    """A named group of states, used for the GMV tiers and for the presets."""

    key: str
    name: str
    codes: tuple[str, ...]


CORE, GROWTH = GMV_TIERS = [
    Tier("core", "Core markets", ("CA", "TX", "FL", "NY")),
    Tier("growth", "Growth markets", ("PA", "IL", "OH", "GA", "NC", "MI", "NJ", "VA")),
]
PRESETS: dict[str, Tier] = {
    preset.key: preset
    for preset in [CORE, Tier("core-growth", "Core + growth", CORE.codes + GROWTH.codes)]
}


def pick_states(state_codes: list[str] | None = None) -> dict[str, str]:
    """Return {state code: zipcode} for all 50 states, or only the codes and presets (e.g. "core") asked for."""
    tokens = [token.strip() for token in state_codes or [] if token.strip()]
    expanded = [code for token in tokens
                for code in (PRESETS[token.lower()].codes if token.lower() in PRESETS else (token,))]
    codes = list(dict.fromkeys(code.upper() for code in expanded))
    if not codes:
        return {code: state.zipcode for code, state in STATES.items()}
    unknown = [code for code in codes if code not in STATES]
    if unknown:
        raise ValueError(f"Unknown state codes: {', '.join(unknown)}")
    return {code: STATES[code].zipcode for code in codes}

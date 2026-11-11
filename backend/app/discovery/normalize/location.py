# ruff: noqa: RUF001
"""Location text -> structured locations + ISO country codes (curated dictionary, India-first)."""

import re
import unicodedata

from app.discovery.contracts import LocationInput

# --------------------------------------------------------------------------------------- countries
_COUNTRIES: dict[str, list[str]] = {
    "IN": ["india", "in", "ind", "bharat"],
    "US": ["united states", "united states of america", "usa", "us", "u s", "u s a", "america", "ame"],
    "GB": ["united kingdom", "uk", "u k", "gb", "gbr", "great britain", "england", "scotland", "wales"],
    "CA": ["canada", "can"],
    "DE": ["germany", "deutschland", "de", "deu"],
    "SG": ["singapore", "sg", "sgp"],
    "IE": ["ireland", "ie", "irl"],
    "NL": ["netherlands", "the netherlands", "holland", "nl", "nld"],
    "PL": ["poland", "pl", "pol"],
    "IL": ["israel", "il", "isr"],
    "JP": ["japan", "jp", "jpn"],
    "AU": ["australia", "au", "aus"],
    "AE": ["united arab emirates", "uae", "are", "ae"],
    "FR": ["france", "fr", "fra"],
    "CH": ["switzerland", "che"],
    "SE": ["sweden", "swe"],
    "ES": ["spain", "esp"],
    "IT": ["italy", "ita"],
    "BR": ["brazil", "bra"],
    "MX": ["mexico", "mex"],
    "CN": ["china", "chn"],
    "HK": ["hong kong", "hkg"],
    "KR": ["south korea", "korea", "kor"],
    "PH": ["philippines", "phl"],
    "MY": ["malaysia", "mys"],
    "ID": ["indonesia", "idn"],
    "TH": ["thailand", "tha"],
    "VN": ["vietnam", "vnm"],
    "ZA": ["south africa", "zaf"],
    "BE": ["belgium", "bel"],
    "RO": ["romania", "rou"],
    "PT": ["portugal", "prt"],
    "AT": ["austria", "aut"],
    "DK": ["denmark", "dnk"],
    "NO": ["norway", "nor"],
    "FI": ["finland", "fin"],
    "CZ": ["czech republic", "czechia", "cze"],
    "NZ": ["new zealand", "nzl"],
    "SA": ["saudi arabia", "sau"],
}
# 2-letter forms that are only accepted when written in upper case as a whole part
_UPPER_ONLY = {"IN", "US", "UK", "GB", "DE", "SG", "IE", "NL", "PL", "IL", "JP", "AU", "AE", "FR", "CA", "NO", "SA"}
COUNTRY_BY_NAME: dict[str, str] = {}
for _code, _names in _COUNTRIES.items():
    for _n in _names:
        COUNTRY_BY_NAME[_n] = _code
ALPHA3 = {n.upper(): c for c, names in _COUNTRIES.items() for n in names if len(n) == 3 and n not in ("uae",)}
ALPHA3.update({"USA": "US", "GBR": "GB", "IND": "IN", "UAE": "AE"})
ALPHA2 = {c for c in _COUNTRIES}

# --------------------------------------------------------------------------------------- regions
IN_STATES = {
    "karnataka": "Karnataka", "maharashtra": "Maharashtra", "tamil nadu": "Tamil Nadu", "telangana": "Telangana",
    "andhra pradesh": "Andhra Pradesh", "kerala": "Kerala", "gujarat": "Gujarat", "rajasthan": "Rajasthan",
    "uttar pradesh": "Uttar Pradesh", "haryana": "Haryana", "west bengal": "West Bengal", "punjab": "Punjab",
    "madhya pradesh": "Madhya Pradesh", "odisha": "Odisha", "orissa": "Odisha", "goa": "Goa", "assam": "Assam",
    "bihar": "Bihar", "jharkhand": "Jharkhand", "chhattisgarh": "Chhattisgarh", "uttarakhand": "Uttarakhand",
    "himachal pradesh": "Himachal Pradesh", "chandigarh": "Chandigarh", "puducherry": "Puducherry",
    "jammu and kashmir": "Jammu and Kashmir",
}  # fmt: skip
IN_STATE_ABBR = {
    "KA": "Karnataka", "MH": "Maharashtra", "TN": "Tamil Nadu", "TG": "Telangana", "TS": "Telangana",
    "AP": "Andhra Pradesh", "KL": "Kerala", "GJ": "Gujarat", "RJ": "Rajasthan", "UP": "Uttar Pradesh",
    "HR": "Haryana", "WB": "West Bengal", "PB": "Punjab", "MP": "Madhya Pradesh", "OD": "Odisha", "OR": "Odisha",
    "DL": "Delhi",
}  # fmt: skip
US_STATES = {
    "california": "California", "texas": "Texas", "new york": "New York", "washington": "Washington",
    "massachusetts": "Massachusetts", "illinois": "Illinois", "georgia": "Georgia", "florida": "Florida",
    "colorado": "Colorado", "virginia": "Virginia", "north carolina": "North Carolina", "oregon": "Oregon",
    "new jersey": "New Jersey", "pennsylvania": "Pennsylvania", "arizona": "Arizona", "ohio": "Ohio",
    "michigan": "Michigan", "minnesota": "Minnesota", "utah": "Utah",
}  # fmt: skip
US_STATE_ABBR = {
    "CA": "California", "TX": "Texas", "NY": "New York", "WA": "Washington", "MA": "Massachusetts",
    "IL": "Illinois", "GA": "Georgia", "FL": "Florida", "CO": "Colorado", "VA": "Virginia",
    "NC": "North Carolina", "OR": "Oregon", "NJ": "New Jersey", "PA": "Pennsylvania", "AZ": "Arizona",
    "OH": "Ohio", "MI": "Michigan", "MN": "Minnesota", "UT": "Utah", "DC": "District of Columbia",
}  # fmt: skip
REGIONS: dict[str, dict[str, str]] = {"IN": IN_STATES, "US": US_STATES}
REGION_ABBR: dict[str, dict[str, str]] = {"IN": IN_STATE_ABBR, "US": US_STATE_ABBR}

# --------------------------------------------------------------------------------------- cities
# canonical city -> (region, country, aliases)
_CITIES: list[tuple[str, str | None, str, list[str]]] = [
    ("Bengaluru", "Karnataka", "IN", ["bangalore", "bengaluru urban", "bangalore urban", "blr", "bengalooru"]),
    ("Hyderabad", "Telangana", "IN", ["hyd", "secunderabad", "cyberabad"]),
    ("Pune", "Maharashtra", "IN", ["poona"]),
    ("Chennai", "Tamil Nadu", "IN", ["madras"]),
    ("Mumbai", "Maharashtra", "IN", ["bombay", "navi mumbai", "thane"]),
    ("Gurugram", "Haryana", "IN", ["gurgaon"]),
    ("Noida", "Uttar Pradesh", "IN", ["greater noida"]),
    ("Delhi", "Delhi", "IN", ["new delhi", "delhi ncr", "ncr", "national capital region", "ghaziabad", "faridabad"]),
    ("Kolkata", "West Bengal", "IN", ["calcutta"]),
    ("Ahmedabad", "Gujarat", "IN", ["gandhinagar"]),
    ("Kochi", "Kerala", "IN", ["cochin", "ernakulam"]),
    ("Thiruvananthapuram", "Kerala", "IN", ["trivandrum"]),
    ("Coimbatore", "Tamil Nadu", "IN", ["kovai"]),
    ("Jaipur", "Rajasthan", "IN", []),
    ("Chandigarh", "Chandigarh", "IN", ["mohali"]),
    ("Indore", "Madhya Pradesh", "IN", []),
    ("Mysuru", "Karnataka", "IN", ["mysore"]),
    ("Nagpur", "Maharashtra", "IN", []),
    ("Lucknow", "Uttar Pradesh", "IN", []),
    ("Bhubaneswar", "Odisha", "IN", []),
    ("Visakhapatnam", "Andhra Pradesh", "IN", ["vizag"]),
    ("Vadodara", "Gujarat", "IN", ["baroda"]),
    ("Surat", "Gujarat", "IN", []),
    ("Bhopal", "Madhya Pradesh", "IN", []),
    ("Mangaluru", "Karnataka", "IN", ["mangalore"]),
    ("Madurai", "Tamil Nadu", "IN", []),
    ("Goa", "Goa", "IN", ["panaji"]),
    ("San Francisco", "California", "US", ["sf", "san francisco bay area", "south san francisco"]),
    ("New York", "New York", "US", ["new york city", "nyc", "brooklyn", "manhattan"]),
    ("Seattle", "Washington", "US", ["bellevue", "redmond", "kirkland"]),
    ("Austin", "Texas", "US", []),
    ("Boston", "Massachusetts", "US", ["cambridge ma"]),
    ("Chicago", "Illinois", "US", []),
    ("Los Angeles", "California", "US", ["la"]),
    ("San Jose", "California", "US", []),
    ("Mountain View", "California", "US", []),
    ("Sunnyvale", "California", "US", []),
    ("Santa Clara", "California", "US", []),
    ("Palo Alto", "California", "US", []),
    ("Menlo Park", "California", "US", []),
    ("Cupertino", "California", "US", []),
    ("San Diego", "California", "US", []),
    ("Atlanta", "Georgia", "US", []),
    ("Dallas", "Texas", "US", []),
    ("Houston", "Texas", "US", []),
    ("Denver", "Colorado", "US", []),
    ("Washington DC", "District of Columbia", "US", ["washington d c", "washington dc"]),
    ("London", None, "GB", []),
    ("Manchester", None, "GB", []),
    ("Edinburgh", None, "GB", []),
    ("Cambridge", None, "GB", []),
    ("Toronto", "Ontario", "CA", []),
    ("Vancouver", "British Columbia", "CA", []),
    ("Montreal", "Quebec", "CA", []),
    ("Ottawa", "Ontario", "CA", []),
    ("Waterloo", "Ontario", "CA", []),
    ("Berlin", None, "DE", []),
    ("Munich", "Bavaria", "DE", ["munchen"]),
    ("Hamburg", None, "DE", []),
    ("Frankfurt", None, "DE", []),
    ("Singapore", None, "SG", []),
    ("Dublin", None, "IE", []),
    ("Cork", None, "IE", []),
    ("Amsterdam", None, "NL", []),
    ("Rotterdam", None, "NL", []),
    ("Warsaw", None, "PL", ["warszawa"]),
    ("Krakow", None, "PL", ["cracow"]),
    ("Wroclaw", None, "PL", []),
    ("Gdansk", None, "PL", []),
    ("Tel Aviv", None, "IL", ["tel aviv yafo"]),
    ("Haifa", None, "IL", []),
    ("Jerusalem", None, "IL", []),
    ("Tokyo", None, "JP", []),
    ("Osaka", None, "JP", []),
    ("Sydney", "New South Wales", "AU", []),
    ("Melbourne", "Victoria", "AU", []),
    ("Brisbane", "Queensland", "AU", []),
    ("Dubai", None, "AE", []),
    ("Abu Dhabi", None, "AE", []),
    ("Paris", None, "FR", []),
    ("Zurich", None, "CH", []),
    ("Stockholm", None, "SE", []),
    ("Madrid", None, "ES", []),
    ("Barcelona", None, "ES", []),
    ("Sao Paulo", None, "BR", []),
    ("Mexico City", None, "MX", []),
    ("Beijing", None, "CN", []),
    ("Shanghai", None, "CN", []),
    ("Shenzhen", None, "CN", []),
    ("Hong Kong", None, "HK", []),
    ("Seoul", None, "KR", []),
    ("Manila", None, "PH", []),
    ("Kuala Lumpur", None, "MY", []),
    ("Jakarta", None, "ID", []),
    ("Bangkok", None, "TH", []),
]  # fmt: skip
CITY_BY_KEY: dict[str, tuple[str, str | None, str]] = {}
for _city, _region, _cc, _aliases in _CITIES:
    for _k in [_city.lower(), *_aliases]:
        CITY_BY_KEY[_k] = (_city, _region, _cc)

_REMOTE_RE = re.compile(r"\b(?:fully\s+)?(?:remote|wfh|work\s+from\s+home|virtual|telecommute)\b", re.I)
_COUNT_ONLY_RE = re.compile(r"^\s*(?:\d+|multiple|various|several)\s+locations?\s*$", re.I)
_SPLIT_RE = re.compile(r"\s*[;|\n/]\s*|\s+(?:and|or)\s+|\s+&\s+")


def _key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace(".", " ")
    s = re.sub(r"\bcity of\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def country_for(text: str) -> str | None:
    """Map an explicit country value ('India', 'IND', 'in', 'IN') to alpha-2."""
    t = text.strip()
    if not t:
        return None
    up = t.upper()
    if len(t) == 2 and up in ALPHA2:
        return up
    if len(t) == 3 and up in ALPHA3:
        return ALPHA3[up]
    return COUNTRY_BY_NAME.get(_key(t))


class _Loc:
    def __init__(self) -> None:
        self.city: str | None = None
        self.region: str | None = None
        self.country: str | None = None
        self.parts: list[str] = []

    def key(self) -> tuple:
        return (
            (self.city, self.region, self.country)
            if (self.city or self.region or self.country)
            else (",".join(self.parts),)
        )


def _classify_country(part: str) -> str | None:
    p = part.strip()
    if not p:
        return None
    k = _key(p)
    if len(k) == 2:
        up = p.strip().upper()
        # two-letter ISO codes only when written upper-case (avoids "in", "us" pronouns / "CA" vs state)
        if p.strip() == up and up in _UPPER_ONLY and up != "CA":
            return COUNTRY_BY_NAME.get(k)
        return None
    if len(k) == 3:
        up = p.strip().upper()
        return ALPHA3.get(up) if p.strip() == up else (COUNTRY_BY_NAME.get(k) if k not in ("can", "ind") else None)
    return COUNTRY_BY_NAME.get(k)


def _parse_chunk(chunk: str) -> list[_Loc]:
    chunk = chunk.replace("(", ",").replace(")", ",")
    chunk = re.sub(r"\s+[-–—]\s+", ", ", chunk)
    tokens: list[str] = []
    for part in chunk.split(","):
        part = part.strip()
        if not part:
            continue
        # Workday style "IND-Bangalore", "USA-CA-San Francisco"
        if "-" in part and " " not in part.split("-")[0]:
            pieces = [p.strip() for p in part.split("-") if p.strip()]
            if pieces and ((pieces[0].upper() in ALPHA3 and pieces[0].isupper()) or pieces[0] in ("IN", "US", "UK")):
                tokens.extend(pieces)
                continue
        tokens.append(part)

    locs: list[_Loc] = []
    cur: _Loc | None = None
    for part in tokens:
        k = _key(part)
        if not k:
            continue
        country_ctx = cur.country if cur else None
        # region abbreviation in context (e.g. "Austin, TX", "Pune, MH")
        if (
            cur
            and country_ctx in REGION_ABBR
            and cur.region is None
            and part.isupper()
            and part in REGION_ABBR[country_ctx]
        ):
            cur.region = REGION_ABBR[country_ctx][part]
            cur.parts.append(part)
            continue
        # region already implied by the city dictionary (e.g. "Bengaluru, Karnataka")
        if cur and cur.region and country_ctx in REGIONS and REGIONS[country_ctx].get(k) == cur.region:
            cur.parts.append(part)
            continue
        # region full name in context after a city (e.g. "Dallas, Georgia")
        if cur and cur.city and cur.region is None and country_ctx in REGIONS and k in REGIONS[country_ctx]:
            cur.region = REGIONS[country_ctx][k]
            cur.parts.append(part)
            continue
        country = _classify_country(part)
        if country:
            if cur is None or (cur.country and cur.country != country):
                cur = _Loc()
                locs.append(cur)
            cur.country = country
            cur.parts.append(part)
            continue
        if k in CITY_BY_KEY:
            city, region, cc = CITY_BY_KEY[k]
            if cur is None or cur.city or (cur.country and cur.country != cc):
                cur = _Loc()
                locs.append(cur)
            cur.city, cur.region, cur.country = city, cur.region or region, cur.country or cc
            cur.parts.append(part)
            continue
        found_region = None
        for cc, table in REGIONS.items():
            if k in table and (country_ctx in (None, cc)):
                found_region = (table[k], cc)
                break
        if found_region:
            if cur is None or cur.region or (cur.country and cur.country != found_region[1]):
                cur = _Loc()
                locs.append(cur)
            cur.region, cur.country = found_region[0], cur.country or found_region[1]
            cur.parts.append(part)
            continue
        # unknown token: remember as raw on the current location (or start an unresolved one)
        if cur is None:
            cur = _Loc()
            locs.append(cur)
        cur.parts.append(part)
    return locs


def parse_locations(text: str | None) -> list[LocationInput]:
    """Parse free location text into structured locations (possibly empty = unknown)."""
    if not text or not text.strip() or _COUNT_ONLY_RE.match(text):
        return []
    out: list[LocationInput] = []
    seen: set[tuple] = set()
    for chunk in _SPLIT_RE.split(text):
        chunk = chunk.strip()
        if not chunk or _COUNT_ONLY_RE.match(chunk):
            continue
        had_remote = bool(_REMOTE_RE.search(chunk))
        stripped = _REMOTE_RE.sub(" ", chunk) if had_remote else chunk
        stripped = re.sub(r"^[\s,\-–:()]+|[\s,\-–:()]+$", "", stripped)
        if had_remote and not stripped:
            locs: list[_Loc] = []
            li = LocationInput(raw="Remote")
            if ("remote",) not in seen:
                seen.add(("remote",))
                out.append(li)
            continue
        locs = _parse_chunk(stripped)
        resolved = [loc for loc in locs if loc.city or loc.region or loc.country]
        if not resolved:
            li = LocationInput(raw=chunk)
            if (chunk.lower(),) not in seen:
                seen.add((chunk.lower(),))
                out.append(li)
            continue
        for loc in resolved:
            k = loc.key()
            if k in seen:
                continue
            seen.add(k)
            raw = chunk if len(resolved) == 1 else ", ".join(loc.parts)
            out.append(LocationInput(raw=raw, city=loc.city, region=loc.region, country_code=loc.country))
    return out


def country_codes(locations: list[LocationInput] | list[dict]) -> list[str]:
    """Unique ISO codes in order of appearance."""
    codes: list[str] = []
    for loc in locations:
        cc = loc.get("country_code") if isinstance(loc, dict) else loc.country_code
        if cc and cc.upper() not in codes:
            codes.append(cc.upper())
    return codes

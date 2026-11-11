import re

# canonical name -> aliases (case-insensitive, matched on word boundaries)
SKILLS: dict[str, list[str]] = {
    "Python": [], "Java": [], "JavaScript": ["JS", "ecmascript"], "TypeScript": [], 
    "Go": ["golang", "go language", "go programming"],
    "Rust": [], "C++": ["cpp"], "C#": ["csharp"], ".NET": ["dotnet", "asp.net"], "Kotlin": [], "Swift": [],
    "Scala": [], "Ruby": ["ruby on rails", "Rails"], "PHP": [], "SQL": [], "PostgreSQL": ["postgres"],
    "MySQL": [], "MongoDB": ["mongo"], "Redis": [], "Kafka": ["apache kafka"], "Spark": ["apache spark", "pyspark"],
    "Hadoop": [], "Airflow": ["apache airflow"], "dbt": [], "Snowflake": [], "BigQuery": [],
    "Docker": [], "Kubernetes": ["k8s"], "Terraform": [], "AWS": ["amazon web services"], "Azure": [],
    "GCP": ["google cloud", "google cloud platform"], "Linux": [], "Git": [], "CI/CD": ["cicd"],
    "Jenkins": [], "React": ["react.js", "reactjs"], "Angular": ["angularjs"], "Vue": ["vue.js", "vuejs"],
    "Node.js": ["nodejs", "node js"], "Django": [], "Flask": [], "FastAPI": [], "Spring Boot": ["springboot"],
    "Spring": [], "GraphQL": [], "REST": ["restful", "rest api", "rest apis"], "gRPC": [],
    "Elasticsearch": ["elastic search"], "TensorFlow": [], "PyTorch": [], "scikit-learn": ["sklearn"],
    "Pandas": [], "NumPy": [], "Machine Learning": ["ML"], "Deep Learning": [], "NLP": ["natural language processing"],
    "LLM": ["llms", "large language models"], "Computer Vision": [], "Data Structures": [],
    "Algorithms": [], "System Design": [], "Microservices": ["microservice"], "Selenium": [],
    "Tableau": [], "Power BI": ["powerbi"], "Excel": [], "Android": [], "iOS": [], "Flutter": [],
    "React Native": [], "RabbitMQ": [], "Cassandra": [], "Oracle": [], "Salesforce": [], "SAP": [],
    "Figma": [], "Agile": [], "Scrum": [], "Jira": [], "HTML": ["html5"], "CSS": ["css3"],
    "Next.js": ["nextjs"], "Redux": [], "Hibernate": [], "Prometheus": [], "Grafana": [],
}  # fmt: skip
# Names that are ambiguous in prose are matched with their exact spelling (case-sensitive)
_CASE_SENSITIVE = {"Swift", "Spring", "Oracle", "Excel", "Scrum", "Agile", "Git", "REST", "ML", "JS", "Rails"}
_SKIP_CANONICAL = {"Go"}  # bare "Go" is a common English word; only golang / "Go language"


def _compile() -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for canon, aliases in SKILLS.items():
        names = ([] if canon in _SKIP_CANONICAL else [canon]) + aliases
        alts = []
        for name in sorted(set(names), key=len, reverse=True):
            esc = re.escape(name).replace(r"\ ", r"[\s-]+")
            alts.append(esc if name in _CASE_SENSITIVE else f"(?i:{esc})")
        pattern = rf"(?<![A-Za-z0-9+#.])(?:{'|'.join(alts)})(?![A-Za-z0-9+#]|\.[A-Za-z0-9])"
        out.append((canon, re.compile(pattern)))
    return out


_COMPILED = _compile()


def extract_skills(*texts: str | None) -> list[str]:
    """Canonical skill names found in the texts (word-boundary matched), ordered by first appearance."""
    blob = "\n".join(t for t in texts if t)
    found: list[tuple[int, str]] = []
    for canon, pat in _COMPILED:
        m = pat.search(blob)
        if m:
            found.append((m.start(), canon))
    found.sort()
    return [c for _, c in found]

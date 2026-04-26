from pathlib import Path

from bs4 import BeautifulSoup


def parse_web(local_path: str) -> str:
    html = Path(local_path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    for tag in ["script", "style", "nav", "footer", "header", "aside"]:
        for node in soup.find_all(tag):
            node.decompose()
    return soup.get_text(separator="\n", strip=True)

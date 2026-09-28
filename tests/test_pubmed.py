import importlib.util
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("harvest", ROOT / "1_harvest_papers.py")
harvest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harvest)


class PubmedTextTest(unittest.TestCase):
    def test_inline_markup_does_not_truncate_scientific_text(self):
        article = ET.fromstring("<PubmedArticle><PMID>123</PMID><ArticleTitle>Effect of <i>gene A</i> in cells</ArticleTitle><Abstract><AbstractText>Expression <b>did not</b> improve function.</AbstractText></Abstract></PubmedArticle>")
        row = harvest._parse_pubmed_article(article)
        self.assertEqual(row["title"], "Effect of gene A in cells")
        self.assertEqual(row["abstract"], "Expression did not improve function.")

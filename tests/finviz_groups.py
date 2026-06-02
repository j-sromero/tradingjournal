import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_groups import build_dynamic_theme, fetch_company_description, fetch_finviz_relations


class TestFinvizPeerDescriptions(unittest.TestCase):
    def test_fetch_root_and_peer_descriptions(self):
        root_ticker = "AAOI"
        root_description = fetch_company_description(root_ticker)
        relations = fetch_finviz_relations(root_ticker)
        peers = relations.get("peers", [])

        peer_items = []
        for peer in peers[:5]:
            desc = fetch_company_description(peer)
            if isinstance(desc, str) and desc.strip():
                peer_items.append((peer, desc))

        theme = build_dynamic_theme(root_description, peer_items, min_support=2, threshold=3)

        print("Theme:", theme["theme_label"])
        print("Anchors:", theme["anchor_keywords"])
        print("Members:", theme["member_tickers"])
        print("Borderline:", theme["borderline_tickers"])
        for row in theme["peer_scores"]:
            print(row["ticker"], row["score"], row["matched_anchors"])

        self.assertTrue(theme["anchor_keywords"])
        self.assertTrue(theme["member_tickers"] or theme["borderline_tickers"])



from market_groups import fetch_company_description, fetch_finviz_relations
# plus the semantic helpers above

def test_dynamic_theme_from_root_and_peers(self):
    root_ticker = "CIEN"
    root_description = fetch_company_description(root_ticker)
    relations = fetch_finviz_relations(root_ticker)
    peers = relations.get("peers", [])

    peer_items = []
    for peer in peers[:5]:
        desc = fetch_company_description(peer)
        if isinstance(desc, str) and desc.strip():
            peer_items.append((peer, desc))

    theme = build_dynamic_theme(root_description, peer_items, min_support=2, threshold=3)

    print("Theme:", theme["theme_label"])
    print("Anchors:", theme["anchor_keywords"])
    print("Members:", theme["member_tickers"])
    print("Borderline:", theme["borderline_tickers"])
    for row in theme["peer_scores"]:
        print(row["ticker"], row["score"], row["matched_anchors"])

    self.assertTrue(theme["anchor_keywords"])
    self.assertTrue(theme["member_tickers"] or theme["borderline_tickers"])

if __name__ == "__main__":
    unittest.main()
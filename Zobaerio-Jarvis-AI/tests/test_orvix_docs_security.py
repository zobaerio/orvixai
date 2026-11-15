import io, json, sys, tempfile, unittest, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.orvix import documents
from jarvis.orvix.control_center import build_snapshot, render_html
from jarvis.orvix.research import research
from jarvis.orvix.security import AuditLog, ConfirmationGate, ConfirmationRequired, RateLimiter, redact

def tmpdir():
    return Path(tempfile.mkdtemp())

class DocumentTests(unittest.TestCase):
    def test_txt_and_csv(self):
        d = tmpdir()
        (d / "a.txt").write_text("Hello world. This is a test document with several sentences. It talks about testing.")
        self.assertIn("Hello", documents.extract_text(d / "a.txt"))
        (d / "d.csv").write_text("name,score\na,10\nb,20\n")
        stats = documents.csv_stats(d / "d.csv")
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["numeric_columns"]["score"]["mean"], 15.0)

    def test_docx_extraction(self):
        d = tmpdir()
        xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>Docx hello content</w:t></w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(d / "x.docx", "w") as zf:
            zf.writestr("word/document.xml", xml)
        self.assertIn("Docx hello", documents.extract_text(d / "x.docx"))

    def test_xlsx_extraction(self):
        d = tmpdir()
        shared = ('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  '<si><t>CellText</t></si></sst>')
        sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c></row></sheetData></worksheet>')
        with zipfile.ZipFile(d / "x.xlsx", "w") as zf:
            zf.writestr("xl/sharedStrings.xml", shared)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        text = documents.extract_text(d / "x.xlsx")
        self.assertIn("CellText", text); self.assertIn("42", text)

    def test_pdf_without_text_layer_is_honest(self):
        d = tmpdir()
        (d / "x.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n")
        with self.assertRaises(documents.DocumentError):
            documents.extract_text(d / "x.pdf")

    def test_unsupported_type(self):
        d = tmpdir()
        (d / "x.bin").write_bytes(b"\x00\x01")
        with self.assertRaises(documents.DocumentError):
            documents.extract_text(d / "x.bin")

    def test_summary_and_qa_and_compare(self):
        text = ("Cats are small domestic animals. Cats sleep for many hours each day. "
                "Dogs are domestic animals too. Dogs need daily walks and exercise.")
        self.assertIn("Cats", documents.summarize_extractive(text, 1))
        self.assertIn("Cats sleep", documents.answer_question(text, "how long do cats sleep"))
        d = tmpdir()
        (d / "a.txt").write_text(text); (d / "b.txt").write_text(text)
        self.assertEqual(documents.compare_documents(d / "a.txt", d / "b.txt")["similarity"], 1.0)

class SecurityTests(unittest.TestCase):
    def test_redact(self):
        self.assertNotIn("sk-abcdefghijklmnop", redact("key sk-abcdefghijklmnop end"))
        self.assertIn("***", redact("Authorization: Bearer abcdefghij"))

    def test_audit_log_redacts_and_tails(self):
        log = AuditLog(tmpdir() / "a.jsonl")
        log.log("test_event", actor="tester", details={"k": "sk-abcdefghijklmnop"})
        tail = log.tail(1)
        self.assertEqual(tail[0]["event"], "test_event")
        self.assertNotIn("sk-abcdefghijklmnop", json.dumps(tail))
        self.assertEqual(log.count(), 1)

    def test_confirmation_gate_fail_closed(self):
        gate = ConfirmationGate()
        with self.assertRaises(ConfirmationRequired):
            gate.check("delete all files", "destructive")
        self.assertTrue(gate.check("read a file", "read"))
        approving = ConfirmationGate(confirmer=lambda a, c: True)
        self.assertTrue(approving.check("delete one file", "destructive"))

    def test_rate_limiter(self):
        limiter = RateLimiter(max_calls=2, window_seconds=60)
        self.assertTrue(limiter.allow("k")); self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))
        self.assertTrue(limiter.allow("other"))

class ResearchTests(unittest.TestCase):
    def test_research_labels_single_source_uncertain(self):
        import jarvis.orvix.research as research_mod
        original = research_mod.fetch_url
        class FakeSource:
            def __init__(self, url, text):
                self.url, self.title, self.text, self.fetched_at = url, "t", text, 0.0
        try:
            research_mod.fetch_url = lambda url: FakeSource(url, "The river Padma flows through Bangladesh and is very wide indeed.")
            report = research_mod.research("Padma river", urls=["https://example.org/a"], use_search=False)
            self.assertEqual(len(report.sources), 1)
            self.assertTrue(all(f["label"] == "single-source / uncertain" for f in report.findings))
        finally:
            research_mod.fetch_url = original

    def test_research_no_sources_raises(self):
        with self.assertRaises(RuntimeError):
            research("anything", urls=[], use_search=False)

class ControlCenterTests(unittest.TestCase):
    def test_snapshot_and_html_use_real_state(self):
        snap = build_snapshot(env={})
        self.assertEqual(len(snap["agents"]), 16)
        openai = next(m for m in snap["models"] if m["provider"] == "openai")
        self.assertFalse(openai["configured"])
        page = render_html(snap)
        self.assertIn("Control Center", page)
        self.assertIn("not configured", page)

if __name__ == "__main__":
    unittest.main()

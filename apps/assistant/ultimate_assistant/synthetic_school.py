from __future__ import annotations


_STYLE = "<style>body{font:16px Segoe UI,sans-serif;max-width:850px;margin:3rem auto;padding:0 1rem;color:#202633}nav a{margin-right:1rem}table{border-collapse:collapse}td,th{border:1px solid #aab;padding:.5rem} .warning{padding:1rem;background:#fff1c2;border:1px solid #d6a929}</style>"
_NAV = "<nav><a href='/demo-school'>Home</a><a href='/demo-school/courses'>Courses</a><a href='/demo-school/assignments'>Assignments</a><a href='/demo-school/grades'>Grades</a></nav>"

PAGES: dict[str, str] = {
    "": f"""<!doctype html><html><head><title>Northstar College Demo</title>{_STYLE}</head><body>
<h1>Northstar Community College — DEMO PORTAL</h1><p class='warning'>SYNTHETIC TRAINING DATA ONLY. No real students, accounts, courses, or grades.</p>
<p>Academic term: Fall 2026. This local sample is designed to test read-only course and assignment discovery.</p>{_NAV}
<h2>Student home</h2><p>Choose Courses, Assignments, or Grades to explore sample records.</p></body></html>""",
    "courses": f"""<!doctype html><html><head><title>Demo Courses</title>{_STYLE}</head><body><h1>Courses — Fall 2026 (DEMO)</h1>{_NAV}
<ul><li><a href='/demo-school/courses/biology-101'>BIO 101 — Biology Foundations</a></li>
<li><a href='/demo-school/courses/history-202'>HIST 202 — Modern World History</a></li>
<li><a href='/demo-school/courses/statistics-110'>STAT 110 — Introductory Statistics</a></li></ul></body></html>""",
    "assignments": f"""<!doctype html><html><head><title>Demo Assignments</title>{_STYLE}</head><body><h1>Assignments — Fall 2026 (DEMO)</h1>{_NAV}
<table><thead><tr><th>Course</th><th>Assignment</th><th>Due</th><th>Status</th></tr></thead><tbody>
<tr><td>BIO 101</td><td>Cell Structure Quiz</td><td>2026-10-05 11:59 PM</td><td>Not started</td></tr>
<tr><td>HIST 202</td><td>Primary Source Reflection</td><td>2026-10-07 5:00 PM</td><td>In progress</td></tr>
<tr><td>STAT 110</td><td>Descriptive Statistics Worksheet</td><td>2026-10-09 11:59 PM</td><td>Not started</td></tr>
<tr><td>BIO 101</td><td>Lab Safety Check</td><td>2026-10-12 11:59 PM</td><td>Submitted</td></tr>
</tbody></table></body></html>""",
    "grades": f"""<!doctype html><html><head><title>Demo Grades</title>{_STYLE}</head><body><h1>Grades — Fall 2026 (DEMO)</h1>{_NAV}
<table><thead><tr><th>Course</th><th>Current grade</th><th>Last updated</th></tr></thead><tbody>
<tr><td>BIO 101 — Biology Foundations</td><td>92.4%</td><td>2026-09-24</td></tr>
<tr><td>HIST 202 — Modern World History</td><td>87.0%</td><td>2026-09-23</td></tr>
<tr><td>STAT 110 — Introductory Statistics</td><td>94.1%</td><td>2026-09-25</td></tr>
</tbody></table></body></html>""",
    "courses/biology-101": f"""<!doctype html><html><head><title>BIO 101 Demo</title>{_STYLE}</head><body><h1>BIO 101 — Biology Foundations</h1>{_NAV}<p class='warning'>SYNTHETIC DEMO DATA ONLY.</p>
<p>Instructor: Dr. Example. Fall 2026 section DEMO-01.</p><h2>Upcoming work</h2>
<p>Cell Structure Quiz — due 2026-10-05 11:59 PM. Current grade: 92.4% (updated 2026-09-24).</p>
<p>Lab Safety Check — submitted 2026-09-20.</p></body></html>""",
    "courses/history-202": f"""<!doctype html><html><head><title>HIST 202 Demo</title>{_STYLE}</head><body><h1>HIST 202 — Modern World History</h1>{_NAV}<p class='warning'>SYNTHETIC DEMO DATA ONLY.</p>
<p>Instructor: Prof. Sample. Fall 2026 section DEMO-02.</p><h2>Upcoming work</h2>
<p>Primary Source Reflection — due 2026-10-07 5:00 PM. Current grade: 87.0% (updated 2026-09-23).</p></body></html>""",
    "courses/statistics-110": f"""<!doctype html><html><head><title>STAT 110 Demo</title>{_STYLE}</head><body><h1>STAT 110 — Introductory Statistics</h1>{_NAV}<p class='warning'>SYNTHETIC DEMO DATA ONLY.</p>
<p>Instructor: Dr. Sample. Fall 2026 section DEMO-03.</p><h2>Upcoming work</h2>
<p>Descriptive Statistics Worksheet — due 2026-10-09 11:59 PM. Current grade: 94.1% (updated 2026-09-25).</p></body></html>""",
}

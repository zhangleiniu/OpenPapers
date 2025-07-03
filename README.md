#  Open-Papers: Research Paper Collection from Top Conferences

Open-Papers is a modular scraping framework designed to collect and organize accepted research papers from major AI/ML/Data Science conferences. It focuses on PDF downloads, metadata curation, and scalable automation.

---

##  Features

- Scrapes full research papers (PDFs) from official conference sites, ECVA, DBLP, OpenReview, etc.
- Metadata parsing: title, authors, year, PDF URL
- Logging & error tracking built-in
- Extensible architecture for adding new conferences

---

## 📁 Project Structure

```
Open-Papers/
├── scrapers/
│   ├── main_scraper.py                   # Entry point for scraping
│   ├── conference_scraper.py             # [Optional] Shared interface
│   └── parsers/
│       ├── aaai_parser.py
│       ├── cikm_parser.py
│       ├── dblp_parser.py                # For DBLP-hosted conferences
│       ├── eccv_parser.py
│       ├── iclr_parser.py
│       ├── icml_parser.py
│       ├── ieee_xplore_parser.py
│       ├── ijcai_parser.py
│       ├── kdd_parser.py
│       ├── neurips_parser.py
│       ├── neurips_old_scraper.py        # NeurIPS 2022–2024
│       └── neurips_2015_2016_scraper.py  # NeurIPS 2015–2016
├── output/                               # Stores generated CSVs / logs
├── notes.md                              # Project progress, handoff, and next steps
├── .gitignore
├── requirements.txt
└── README.md
```
---

## ✅ Supported Conferences & Years

| Conference | Years Covered | Source | Status | Notes |
|------------|----------------|--------|--------|-------|
| **AAAI**   | 2000–2024      | DBLP + AAAI | Pending | Combined source scraping with direct PDF links. |
| **CIKM**   | 2006–2024      | Conference portal | Pending | Metadata + PDFs where possible. |
| **CVPR**   | 2000–2024      | DBLP/IEEE | Pending | Collected using IEEE Xplore / DBLP. |
| **ECCV**   | 2020–2024| ECVA | ✅ Done | PDF links extracted from ECVA. |
| **ICML**   | 2010–2024      | Public archives | Pending| Structured format helped batch download. |
| **ICLR**   | 2018–2023      | PaperCopilot + OpenReview | ✅ Done | JSON-based metadata, full PDFs downloaded. 2013–2017 WIP. |
| **IEEE Conf.** | 2022–2024 | IEEE Xplore | Pending | PDFs downloaded via Xplore (where accessible). |
| **IJCAI**  | 2010–2024      | DBLP | ✅ Done | Used `dblp_parser.py`. |
| **KDD**    | 2010–2024      | Conference pages / Semantic Scholar | ⚠️ Partial | In progress — added fallback using Semantic Scholar metadata, but coverage is incomplete. |
| **NeurIPS**| 2015–2016, 2021–2024 | papers.nips.cc | ⚠️ Partial | Some PDFs may not be downloadable if hosted outside OpenReview (e.g., IEEE). These cases are skipped and logged |

---

## ️ How to Run

### To run **all available scrapers**:
```bash
python3 -m scrapers.main_scraper

To run a specific conference/year:
PYTHONPATH=$(pwd) python3 -m scrapers.main_scraper --conference <CONFERENCE_NAME> --year <YEAR>

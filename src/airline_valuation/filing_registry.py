"""Code-owned trust pins for the two locally authorized SEC filing fixtures."""
from __future__ import annotations

REGISTRY_VERSION = "airline-sec-filing-fixtures/1"

FILINGS = {
    "southwest": {
        "cik": "0000092380", "accession": "0000092380-26-000004", "form": "10-K",
        "report_date": "2025-12-31", "primary_document": "luv-20251231.htm",
        "artifacts": {
            "submissions": {"filename": "southwest_submissions.json", "url": "https://data.sec.gov/submissions/CIK0000092380.json", "content_type": "application/json", "length": 170867, "sha256": "8bf0f0a60879606b2c4c202eaf8e1c53f36c1591e48108d1655b111b1a50cde7"},
            "index": {"filename": "southwest_index.json", "url": "https://www.sec.gov/Archives/edgar/data/92380/000009238026000004/index.json", "content_type": "application/json", "length": 14620, "sha256": "22754cb1252a66ebaf4e45f1714d306abce73b3e07f456cf7919da1804a259ef"},
            "primary": {"filename": "southwest_primary.html", "url": "https://www.sec.gov/Archives/edgar/data/92380/000009238026000004/luv-20251231.htm", "content_type": "text/html", "length": 2847524, "sha256": "acef06cee393ccf8fcd1e5f47c1decd3a2d04aa73e51a4cbb5f64d94925b08af"},
        },
    },
    "delta": {
        "cik": "0000027904", "accession": "0000027904-26-000013", "form": "10-K",
        "report_date": "2025-12-31", "primary_document": "dal-20251231.htm",
        "artifacts": {
            "submissions": {"filename": "delta_submissions.json", "url": "https://data.sec.gov/submissions/CIK0000027904.json", "content_type": "application/json", "length": 154789, "sha256": "e9980cea6913fd09759e94cba1d45c3d14f20f30b4b66eaf965cad16c71d2485"},
            "index": {"filename": "delta_index.json", "url": "https://www.sec.gov/Archives/edgar/data/27904/000002790426000013/index.json", "content_type": "application/json", "length": 11375, "sha256": "5166ecf5dfbea91b50a87715c5dc0f84263bc4269d2917ebbb1be6bb2e6c5e1a"},
            "primary": {"filename": "delta_primary.html", "url": "https://www.sec.gov/Archives/edgar/data/27904/000002790426000013/dal-20251231.htm", "content_type": "text/html", "length": 2534164, "sha256": "3368116cae1fa67b9f7f8480eabf7670b98cd1993a283dfdd6de68b75fa4d6e4"},
        },
    },
    "american": {
        "cik": "0000006201", "accession": "0000006201-26-000014", "form": "10-K",
        "report_date": "2025-12-31", "primary_document": "aal-20251231.htm",
        "artifacts": {
            "submissions": {"filename": "american_submissions.json", "url": "https://data.sec.gov/submissions/CIK0000006201.json", "content_type": "application/json", "length": 166318, "sha256": "d824dd45d009e4d3a6931c79e43387347a4d064e6f6b19b8dde4083133ae4081"},
            "index": {"filename": "american_index.json", "url": "https://www.sec.gov/Archives/edgar/data/6201/000000620126000014/index.json", "content_type": "application/json", "length": 15165, "sha256": "cc134e0a009c87b3e425880d134c55936f116a5ff9108e6262c72e63e830bb32"},
            "primary": {"filename": "american_primary.html", "url": "https://www.sec.gov/Archives/edgar/data/6201/000000620126000014/aal-20251231.htm", "content_type": "text/html", "length": 4938214, "sha256": "48c85bab448be08e4d8883a270b06d51c6b0d33b369ffcbb1d892d3209cfc0c8"},
        },
    },
    "united": {
        "cik": "0000100517", "accession": "0000100517-26-000023", "form": "10-K",
        "report_date": "2025-12-31", "primary_document": "ual-20251231.htm",
        "artifacts": {
            "submissions": {"filename": "united_submissions.json", "url": "https://data.sec.gov/submissions/CIK0000100517.json", "content_type": "application/json", "length": 162200, "sha256": "cae8ad329f5cbdc6d382e5cd6dbd4d8ff867405c5d1e769dd18051f18883ed7a"},
            "index": {"filename": "united_index.json", "url": "https://www.sec.gov/Archives/edgar/data/100517/000010051726000023/index.json", "content_type": "application/json", "length": 12575, "sha256": "1b1b23ef9965545ea5a669bf2c507736f53c076da2bac8a496cfb6cc921d6785"},
            "primary": {"filename": "united_primary.html", "url": "https://www.sec.gov/Archives/edgar/data/100517/000010051726000023/ual-20251231.htm", "content_type": "text/html", "length": 2932991, "sha256": "5282ce1aeb4a326e16082e291b5637d21a4c7df7ac2c120ef50e97c3ed9ee1df"},
        },
    },
}

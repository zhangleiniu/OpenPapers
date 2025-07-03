# external/filtered_aaai_scraper.py

import os
import json
import re
import time
import requests
from pathlib import Path
from tqdm import tqdm
from config import (
    PAPERS_DIR, METADATA_DIR, CONFERENCES,
    DEFAULT_REQUEST_DELAY, DEFAULT_RETRY_ATTEMPTS, DEFAULT_TIMEOUT, USER_AGENT
)

# === Environment-based directory config ===
PAPERCOPILOT_DIR = Path(os.getenv("PAPERCOPILOT_ROOT", "./data")) 

# === Settings ===
BATCH_SIZE = 10  # Save metadata every N files

# === Status filtering configurations ===
STATUS_FILTERS = {
    'iclr': {
        'accepted_statuses': [
            # Core accepted categories
            'Poster',           # Main acceptance category
            'Oral',            # High-quality oral presentations
            'Spotlight',       # Notable papers (2020-2022, 2024)
            'Talk',            # Alternative to Oral in 2020
            'Top-5%',          # Top tier papers (2023, 2025)
            'Top-25%',         # Good papers (2023, 2025)
            # Legacy or alternative names
            'Accept (Poster)',
            'Accept (Oral)', 
            'Accept (Spotlight)',
            'Workshop',        # Workshop papers (might want to exclude these)
            'Published'
        ],
        'rejected_statuses': [
            'Reject',
            'Withdraw',        # Note: "Withdraw" not "Withdrawn"
            'Desk Reject',
            'Active'           # Unclear status, appears rarely
        ],
        # Special category for workshop papers (you might want these separate)
        'workshop_statuses': [
            'Workshop'
        ]
    }
    # Add more conferences as needed
}

# === Helpers ===
def sanitize_filename(s):
    return re.sub(r'[^\w\s\-\.]', '', s).replace(" ", "_")[:80]

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_conference_config(conference):
    """Get configuration for a specific conference"""
    config = CONFERENCES.get(conference, {})
    return {
        'request_delay': config.get('request_delay', DEFAULT_REQUEST_DELAY),
        'retry_attempts': config.get('retry_attempts', DEFAULT_RETRY_ATTEMPTS),
        'timeout': config.get('timeout', DEFAULT_TIMEOUT),
        'rate_limit_delay': config.get('rate_limit_delay', 60),
        'name': config.get('name', conference.upper())
    }

def should_include_paper(paper, conference, filter_mode='accepted_only'):
    """
    Determine if a paper should be included based on its status
    
    Args:
        paper: Paper dictionary
        conference: Conference name
        filter_mode: 'accepted_only', 'accepted_no_workshop', 'all', 'rejected_only', 
                    'high_quality_only', 'workshop_only', or 'custom'
    """
    if filter_mode == 'all':
        return True
    
    status = paper.get('status', '').strip()
    
    # Handle conferences without status fields (like AAAI)
    if not status or conference not in STATUS_FILTERS:
        # If no status field or conference not in filter config, assume it's accepted
        # This maintains backward compatibility with AAAI and other conferences
        return filter_mode in ['accepted_only', 'accepted_no_workshop', 'high_quality_only', 'custom']
    
    filter_config = STATUS_FILTERS.get(conference, {})
    accepted_statuses = filter_config.get('accepted_statuses', [])
    rejected_statuses = filter_config.get('rejected_statuses', [])
    workshop_statuses = filter_config.get('workshop_statuses', [])
    
    if filter_mode == 'accepted_only':
        return status in accepted_statuses
    elif filter_mode == 'accepted_no_workshop':
        return status in accepted_statuses and status not in workshop_statuses
    elif filter_mode == 'rejected_only':
        return status in rejected_statuses
    elif filter_mode == 'workshop_only':
        return status in workshop_statuses
    elif filter_mode == 'high_quality_only':
        # Only top-tier papers: Oral, Spotlight, Top-5%, Top-25%
        high_quality = ['Oral', 'Spotlight', 'Talk', 'Top-5%', 'Top-25%']
        return status in high_quality
    elif filter_mode == 'custom':
        # For custom filtering, exclude rejected but include everything else
        return status not in rejected_statuses
    
    return True

def get_pdf_url(paper, conference):
    if conference == 'iclr':
        return f"https://openreview.net/pdf?id={paper['id']}"
    else:
        return paper.get('pdf')
    
def download_pdf(url, save_path, conference_config):
    """Download PDF with conference-specific settings"""
    headers = {'User-Agent': USER_AGENT}
    
    for attempt in range(conference_config['retry_attempts']):
        try:
            r = requests.get(
                url, 
                timeout=conference_config['timeout'], 
                stream=True,
                headers=headers
            )
            r.raise_for_status()
            
            total_size = int(r.headers.get('content-length', 0))
            with open(save_path, "wb") as f:
                if total_size > 0:
                    with tqdm(
                        total=total_size, 
                        unit='B', 
                        unit_scale=True, 
                        desc=f"Downloading {save_path.name}",
                        leave=False
                    ) as pbar:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                pbar.update(len(chunk))
                else:
                    f.write(r.content)
            
            # Use conference-specific delay
            time.sleep(conference_config['request_delay'])
            return True
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:  # Rate limited
                print(f"⏳ Rate limited, waiting {conference_config['rate_limit_delay']}s...")
                time.sleep(conference_config['rate_limit_delay'])
                continue
            else:
                print(f"❌ HTTP error {e.response.status_code} for {url}")
                break
        except requests.exceptions.Timeout:
            print(f"⏰ Timeout (attempt {attempt + 1}/{conference_config['retry_attempts']}) for {url}")
            if attempt < conference_config['retry_attempts'] - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            else:
                break
        except Exception as e:
            print(f"❌ Failed to download {url}: {e}")
            break
    
    return False

def process_conference_year(conference, year, filter_mode='accepted_only', start_index=0):
    """Process a single conference year with status filtering"""
    conference_config = get_conference_config(conference)
    
    raw_path = PAPERCOPILOT_DIR / conference / f"{conference}{year}.json"
    output_path = METADATA_DIR / conference/ f"{conference}_{year}.json"
    save_dir = PAPERS_DIR / conference / str(year)
    save_dir.mkdir(parents=True, exist_ok=True)
    
    if not raw_path.exists():
        print(f"❌ Raw data file not found: {raw_path}")
        return []

    papers = load_json(raw_path)
    
    # Filter papers by status first
    original_count = len(papers)
    
    if conference in STATUS_FILTERS and filter_mode != 'all':
        papers = [p for p in papers if should_include_paper(p, conference, filter_mode)]
        filtered_count = len(papers)
        print(f"📊 Filtered {original_count} → {filtered_count} papers based on status")
        
        # Show status breakdown for conferences with status fields
        if filtered_count > 0:
            status_counts = {}
            for paper in papers:
                status = paper.get('status', 'No Status')
                status_counts[status] = status_counts.get(status, 0) + 1
            print(f"📈 Status breakdown: {dict(sorted(status_counts.items()))}")
    else:
        # For conferences without status filtering (like AAAI) or 'all' mode
        if conference not in STATUS_FILTERS:
            print(f"📊 No status filtering for {conference} - assuming all {original_count} papers are accepted")
        elif filter_mode == 'all':
            print(f"📊 Filter mode 'all' - including all {original_count} papers")
        filtered_count = original_count
    
    # Load existing cleaned data to resume from where we left off
    existing_cleaned = load_json(output_path)
    existing_papers_by_id = {paper.get("id"): paper for paper in existing_cleaned}
    existing_ids = {paper.get("id") for paper in existing_cleaned}
    
    cleaned = []
    processed_count = 0
    successful_downloads = 0
    skipped_downloads = 0
    retry_downloads = 0

    # Filter out already processed papers
    papers_to_process = []
    papers_needing_pdf_retry = []
    # papers_to_process = [p for p in papers[start_index:] if p.get("id") not in existing_ids]
    
    for paper in papers[start_index:]:
        paper_id = paper.get("id")
        if paper_id in existing_ids:
            existing_paper = existing_papers_by_id[paper_id]
            # Check if PDF download failed previously
            if (existing_paper.get("pdf_downloaded") == False or 
                existing_paper.get("pdf_path") is None) and paper.get("pdf"):
                papers_needing_pdf_retry.append((paper, existing_paper))
            else:
                # Paper is complete, keep as-is
                cleaned.append(existing_paper)
        else:
            # New paper to process
            papers_to_process.append(paper)
    
    total_to_process = len(papers_to_process) + len(papers_needing_pdf_retry)
    print(f"📊 Found {len(papers)} filtered papers, {len(cleaned)} already completed, {len(papers_needing_pdf_retry)} need PDF retry, {len(papers_to_process)} new papers")

    # Process papers needing PDF retry first
    if papers_needing_pdf_retry:
        print(f"🔄 Retrying PDF downloads for {len(papers_needing_pdf_retry)} papers...")
        retry_downloads
        with tqdm(papers_needing_pdf_retry, desc=f"Retrying PDF downloads for {conference_config['name']} {year}") as pbar:
            for original_paper, existing_paper in pbar:
                try:
                    pdf_url = get_pdf_url(original_paper, conference)
                    if not pdf_url:
                        # No PDF URL, keep existing data
                        cleaned.append(existing_paper)
                        continue
                    
                    # Reconstruct PDF path
                    title = existing_paper["title"]
                    id_ = existing_paper["id"]
                    safe_title = sanitize_filename(title)
                    pdf_filename = f"{id_}_{safe_title}.pdf"
                    pdf_path = save_dir / pdf_filename
                    
                    # Update progress bar
                    pbar.set_postfix({
                        'retries': retry_downloads,
                        'current': title[:25] + "..." if len(title) > 25 else title
                    })
                    
                    # Try to download PDF
                    pdf_downloaded = download_pdf(pdf_url, pdf_path, conference_config)
                    if pdf_downloaded:
                        retry_downloads += 1
                        # Update existing paper data
                        existing_paper["pdf_path"] = str(pdf_path)
                        existing_paper["pdf_downloaded"] = True
                        print(f"✅ Successfully downloaded PDF for: {title[:50]}...")
                    
                    cleaned.append(existing_paper)
                    processed_count += 1
                    
                    # Save progress every BATCH_SIZE papers
                    if processed_count % BATCH_SIZE == 0:
                        save_json(output_path, cleaned)
                        pbar.set_description(f"Retrying PDFs for {conference_config['name']} {year} (saved)")
                        
                except Exception as e:
                    print(f"⚠️ Error retrying PDF for paper {existing_paper.get('title', 'Unknown')}: {e}")
                    # Keep existing data even if retry fails
                    cleaned.append(existing_paper)
                    continue

    with tqdm(papers_to_process, desc=f"Processing {conference_config['name']} {year}") as pbar:
        for i, paper in enumerate(pbar):
            try:
                title = paper["title"]
                id_ = paper.get("id") or paper.get("doi", "").split(".")[-1]
                authors = [a.strip() for a in paper.get("author", "").split(";") if a.strip()]
                abstract = paper.get("abstract", "")
                pdf_url = get_pdf_url(paper, conference)
                site_url = paper.get("site")
                status = paper.get("status", "")
                track = paper.get("track", "")

                safe_title = sanitize_filename(title)
                pdf_filename = f"{id_}_{safe_title}.pdf"
                pdf_path = save_dir / pdf_filename

                # Update progress bar description
                pbar.set_postfix({
                    'downloads': successful_downloads,
                    'skipped': skipped_downloads,
                    'status': status[:15] + "..." if len(status) > 15 else status
                })

                # Download PDF if it doesn't exist and URL is available
                pdf_downloaded = pdf_path.exists()
                if pdf_downloaded:
                    skipped_downloads += 1
                elif pdf_url:
                    pdf_downloaded = download_pdf(pdf_url, pdf_path, conference_config)
                    if pdf_downloaded:
                        successful_downloads += 1

                # Add to cleaned data regardless of PDF download success
                cleaned.append({
                    "id": id_,
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "pdf_url": pdf_url,
                    "year": year,
                    "conference": conference,
                    "url": site_url,
                    "pdf_path": str(pdf_path) if pdf_downloaded else None,
                    "pdf_downloaded": pdf_downloaded,
                    "status": status,
                    "track": track,
                    "filter_mode": filter_mode
                })

                processed_count += 1

                # Save progress every BATCH_SIZE papers
                if processed_count % BATCH_SIZE == 0:
                    save_json(output_path, cleaned)
                    pbar.set_description(f"Processing {conference_config['name']} {year} (saved)")

            except KeyboardInterrupt:
                print("\n⏸️ Interrupted by user")
                save_json(output_path, cleaned)
                raise
            except Exception as e:
                print(f"⚠️ Error processing paper {paper.get('title', 'Unknown')}: {e}")
                continue

    # Final save
    save_json(output_path, cleaned)
    print(f"✅ Saved {len(cleaned)} papers to {output_path}")
    print(f"📥 Successfully downloaded {successful_downloads} PDFs")
    print(f"🔄 Successfully retried {retry_downloads} PDF downloads")
    print(f"⏭️ Skipped {skipped_downloads} existing PDFs")
    

def process_conferences(conference, years, filter_mode='accepted_only'):
    """
    Process multiple conferences and years with filtering
    
    Args:
        conferences:
        years: 
        filter_mode: 
            - 'accepted_only': All accepted papers including workshop
            - 'accepted_no_workshop': Accepted papers excluding workshop
            - 'high_quality_only': Only Oral, Spotlight, Top-5%, Top-25%
            - 'workshop_only': Only workshop papers
            - 'all': All papers regardless of status
            - 'rejected_only': Only rejected papers
            - 'custom': Custom logic
        start_from: tuple like ('aaai', 2022) to resume from a specific conference/year
    """
    
    print(f"🎯 Processing with filter mode: {filter_mode}")
    
    # Validate conference exists in config
    if conference not in CONFERENCES:
        print(f"⚠️ Warning: Conference '{conference}' not found in config, using defaults")
    
    for year in years:
        try:
            process_conference_year(conference, year, filter_mode)
        except KeyboardInterrupt:
            print(f"\n⏸️ Interrupted during {conference} {year}")
            print(f"💡 To resume, use start_from=('{conference}', {year})")
        except Exception as e:
            print(f"❌ Error processing {conference} {year}: {e}")
            continue
    
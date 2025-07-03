from dotenv import load_dotenv
load_dotenv()

from external.papercopilot import process_conferences
from config import CONFERENCES

if __name__ == "__main__":
    try:
        # process_conferences('aaai', [2021, 2022, 2023, 2024, 2025], filter_mode='all')
        # process_conferences('iclr', [2025], filter_mode='accepted_only')
        process_conferences('icml', [2025], filter_mode='all')
        
    except KeyboardInterrupt:
        print("\n⏹️ Processing interrupted by user")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()

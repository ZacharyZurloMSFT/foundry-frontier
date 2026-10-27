"""Package entrypoint shared by every hosted insurance role; AGENT_ROLE selects which one runs."""
from agents.hosted.main import main

if __name__ == "__main__":
    main()

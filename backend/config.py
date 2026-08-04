import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    APP_NAME: str = "Ruijie Router Monitor"
    
    # Router Credentials & Address
    RUIJIE_HOST: str = os.getenv("RUIJIE_HOST", "http://192.168.8.1")
    RUIJIE_USER: str = os.getenv("RUIJIE_USER", "admin")
    RUIJIE_PASS: str = os.getenv("RUIJIE_PASS", "a123456789.")
    
    # Mode: 'live' or 'demo'
    COLLECTOR_MODE: str = os.getenv("COLLECTOR_MODE", "live")
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "3"))  # seconds
    
    # Telegram Notifications
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLE: bool = os.getenv("TELEGRAM_ENABLE", "false").lower() == "true"

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/monitor.db")

    def save(self):
        env_file = os.path.join(os.path.dirname(__file__), ".env")
        lines = []
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                
        # Parse existing
        env_dict = {}
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_dict[k] = v
                
        # Update
        env_dict["RUIJIE_HOST"] = self.RUIJIE_HOST
        env_dict["RUIJIE_USER"] = self.RUIJIE_USER
        env_dict["RUIJIE_PASS"] = self.RUIJIE_PASS
        env_dict["COLLECTOR_MODE"] = self.COLLECTOR_MODE
        env_dict["POLL_INTERVAL"] = str(self.POLL_INTERVAL)
        env_dict["TELEGRAM_BOT_TOKEN"] = self.TELEGRAM_BOT_TOKEN
        env_dict["TELEGRAM_CHAT_ID"] = self.TELEGRAM_CHAT_ID
        env_dict["TELEGRAM_ENABLE"] = str(self.TELEGRAM_ENABLE).lower()
        env_dict["DATABASE_URL"] = self.DATABASE_URL
        
        # Write back
        with open(env_file, "w", encoding="utf-8") as f:
            for k, v in env_dict.items():
                f.write(f"{k}={v}\n")

settings = Settings()

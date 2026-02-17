from app import app, boot_application

# Initialize database and start background pinger once per process.
boot_application(start_background=True)

application = app

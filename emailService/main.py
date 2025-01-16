import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import smtplib
from typing import Dict, List, Optional, Union
import time
from datetime import datetime
import logging
from pathlib import Path

class EmbassyEmailer:
    def __init__(self):
        self.email_user = os.getenv('EMAIL_USER')
        self.email_password = os.getenv('EMAIL_PASSWORD')
        self.sent_log = 'sent_emails.json'
        self.initialize_logging()
        
        # Check if credentials are set
        if not self.email_user or not self.email_password:
            raise ValueError("EMAIL_USER and EMAIL_PASSWORD environment variables must be set!")

    def initialize_logging(self):
        # Log to both file and console
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('embassy_emailer.log'),
                logging.StreamHandler()
            ]
        )

    def load_sent_log(self) -> Dict:
        """Load the log of already sent emails."""
        try:
            if os.path.exists(self.sent_log):
                with open(self.sent_log, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logging.error(f"Error loading sent log: {e}")
            return {}

    def save_sent_log(self, sent_data: Dict):
        """Save the updated sent emails log."""
        try:
            with open(self.sent_log, 'w', encoding='utf-8') as f:
                json.dump(sent_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.error(f"Error saving sent log: {e}")

    def load_embassy_data(self, file_path: str) -> Dict:
        """Load embassy email data from JSON file."""
        try:
            logging.info(f"Loading embassy data from {file_path}...")
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logging.info(f"Loaded data for {len(data)} countries")
            return data
        except Exception as e:
            logging.error(f"Error loading embassy data: {e}")
            return {}

    def add_attachment(self, msg: MIMEMultipart, file_path: Union[str, Path]) -> bool:
        """
        Add an attachment to the email message.
        
        Args:
            msg: The email message object
            file_path: Path to the file to attach
            
        Returns:
            bool: True if attachment was successful, False otherwise
        """
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                logging.error(f"Attachment file not found: {file_path}")
                return False
                
            with open(file_path, 'rb') as f:
                attachment = MIMEApplication(f.read(), _subtype=file_path.suffix[1:])
                
            attachment.add_header(
                'Content-Disposition', 
                'attachment', 
                filename=file_path.name
            )
            msg.attach(attachment)
            logging.info(f"Successfully attached file: {file_path.name}")
            return True
            
        except Exception as e:
            logging.error(f"Failed to add attachment {file_path}: {e}")
            return False

    def send_email(
        self, 
        recipient_email: str, 
        country: str, 
        attachments: Optional[List[Union[str, Path]]] = None
    ) -> bool:
        """
        Send an email to an embassy with optional attachments.
        
        Args:
            recipient_email: Email address of the recipient
            country: Name of the country
            attachments: Optional list of file paths to attach
        """
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_user
            msg['To'] = recipient_email
            msg['Subject'] = f"Visa Inquiry - US Refugee Travel Document - {country}"

            # Email body
            body = f"""
Dear {country} Embassy,

I hope this email finds you well. I am writing to inquire about the visa requirements for holders of US Refugee Travel Document issued by USCIS.
Specifically, I would like to know:

1. Whether your country recognizes and accepts the US Refugee Travel Document
2. If a visa is required for entry with this travel document
3. If required, what is the process for obtaining a visa

I have attached a sample of the US Refugee Travel Document for your reference. Thank you for your assistance.

Best regards,
Mason
            """

            msg.attach(MIMEText(body, 'plain'))

            # Add attachments if provided
            if attachments:
                for attachment_path in attachments:
                    if not self.add_attachment(msg, attachment_path):
                        logging.warning(f"Failed to add attachment: {attachment_path}")

            # Connect to Gmail's SMTP server
            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(self.email_user, self.email_password)
                server.send_message(msg)

            logging.info(f"Email sent successfully to {country} ({recipient_email})")
            return True

        except Exception as e:
            logging.error(f"Failed to send email to {country} ({recipient_email}): {e}")
            return False

    def process_embassies(
        self, 
        data_file: str, 
        batch_size: int = 10,
        attachments: Optional[List[Union[str, Path]]] = None
    ):
        """
        Process embassies and send emails in batches.
        
        Args:
            data_file: Path to the JSON file containing embassy data
            batch_size: Number of emails to send before taking a break
            attachments: Optional list of file paths to attach to each email
        """
        embassy_data = self.load_embassy_data(data_file)
        sent_log = self.load_sent_log()
        
        total_countries = len(embassy_data)
        processed = 0
        
        logging.info(f"Starting to process {total_countries} countries...")
        logging.info(f"Already sent to {len(sent_log)} countries")
        
        try:
            for country, info in embassy_data.items():
                processed += 1
                logging.info(f"Processing country {processed}/{total_countries}: {country}")
                
                if country in sent_log:
                    logging.info(f"Skipping {country} - already contacted")
                    continue

                if 'emails' not in info or not info['emails']:
                    logging.warning(f"No email found for {country}")
                    continue

                logging.info(f"Found {len(info['emails'])} email(s) for {country}")
                
                # Take only the first email from the list
                if info['emails']:
                    email = info['emails'][0]  # Get first email
                    logging.info(f"Sending email to {country} ({email})...")
                    if self.send_email(email, country, attachments):
                        sent_log[country] = {
                            'email': email,
                            'sent_date': datetime.now().isoformat(),
                            'status': 'sent'
                        }
                        self.save_sent_log(sent_log)
                        logging.info(f"Successfully sent email to {country}")
                        
                        logging.info("Waiting 60 seconds before next email...")
                        time.sleep(60)  # 1 minute delay

                if processed % batch_size == 0:
                    logging.info(f"Processed {processed} out of {total_countries} countries")
                    logging.info(f"Taking a 1 hour break...")
                    time.sleep(3600)

        except KeyboardInterrupt:
            logging.warning("Process interrupted by user. Progress saved.")
        except Exception as e:
            logging.error(f"Error occurred: {e}")
        finally:
            logging.info(f"Process completed. Processed {processed} countries.")
            logging.info(f"Successfully sent emails to {len(sent_log)} countries.")

if __name__ == "__main__":
    try:
        print("Starting Embassy Email Service...")
        emailer = EmbassyEmailer()
        
        # Set up correct file paths
        current_dir = os.path.dirname(os.path.abspath(__file__))
        scraper_dir = os.path.join(current_dir, '..', 'emailScrapper')
        
        # Embassy data file path
        embassy_file = os.path.join(scraper_dir, 'embassy_emails.json')
        if not os.path.exists(embassy_file):
            print(f"Error: {embassy_file} not found!")
            exit(1)
            
        # PDF attachment path
        pdf_path = os.path.join(current_dir, 'RefugeeTravelDocument.pdf')
        if not os.path.exists(pdf_path):
            print(f"Error: {pdf_path} not found!")
            exit(1)
            
        attachments = [pdf_path]
            
        # Start the process with attachments
        emailer.process_embassies(embassy_file, batch_size=10, attachments=attachments)
        
    except Exception as e:
        print(f"Critical error: {e}")
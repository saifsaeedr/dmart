from sys import modules as sys_modules
import json
from pathlib import Path
from models.core import Content, PluginBase, Event, ActionType, Ticket, User
from models.enums import ResourceType
from utils.helpers import camel_case
from utils.settings import settings
from data_adapters.adapter import data_adapter as db
from fastapi.logger import logger
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def _load_plugin_config():
    """Load SMTP config from plugin's config.json file"""
    config_file = Path(__file__).parent / "config.json"
    try:
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
                return config.get("smtp_config", {})
    except Exception as e:
        logger.error(f"Failed to load plugin config: {e}")
    return {}


# Load plugin config once at module import
_plugin_smtp_config = _load_plugin_config()


async def send_email_smtp(
    from_address: str,
    to_address: str,
    message: str,
    subject: str,
    from_name: str = ""
) -> bool:
    """Send email using SMTP from plugin config.json"""
    try:
        # Read SMTP configuration from plugin config.json only
        mail_host = _plugin_smtp_config.get("host", "")
        mail_port = int(_plugin_smtp_config.get("port", 587))
        mail_username = _plugin_smtp_config.get("username", "")
        mail_password = _plugin_smtp_config.get("password", "")
        
        if not mail_host:
            logger.error("SMTP host not configured in plugin config.json")
            return False
        
        # Create message
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{from_name} <{from_address}>" if from_name else from_address
        msg["To"] = to_address
        msg["Subject"] = subject
        
        html_part = MIMEText(message, "html")
        msg.attach(html_part)
        
        # Standard encryption: SSL for port 465, STARTTLS for other ports
        use_ssl = mail_port == 465
        
        # Create and connect SMTP
        smtp = aiosmtplib.SMTP(hostname=mail_host, port=mail_port, use_tls=use_ssl)
        await smtp.connect()
        
        # Start TLS for non-SSL connections (port 587)
        if not use_ssl:
            try:
                await smtp.starttls()
            except Exception as e:
                # If TLS is already active, that's fine - continue
                if "already using tls" not in str(e).lower():
                    raise
        
        # Authenticate
        if mail_username and mail_password:
            await smtp.login(mail_username, mail_password)
        
        # Send email
        await smtp.send_message(msg)
        
        # Close connection (ignore quit errors)
        try:
            await smtp.quit()
        except Exception:
            pass
        
        logger.info(f"Sent email to {to_address} for ticket notification")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send email to {to_address}: {e}")
        return False


class Plugin(PluginBase):
    async def hook(self, data: Event):
        print("heyy i raaaaan ------------------------------")
        print(f"DEBUG: resource_type={data.resource_type}, action_type={data.action_type}, action_type type={type(data.action_type)}")
        """
        Send email notifications to users when they are added to a ticket's ACL
        via update_acl request.
        """
        # Check if this is a ticket resource
        if data.resource_type != ResourceType.content:
            print(f"DEBUG: Returning early - resource_type mismatch: {data.resource_type} != {ResourceType.content}")
            return

        # Check if this is an update action
        print(f"DEBUG: Checking action_type: {data.action_type} vs {ActionType.update}")
        if data.action_type != ActionType.update:
            print(f"DEBUG: Returning early - action_type mismatch: {data.action_type} != {ActionType.update}")
            return

        # Check if history_diff contains ACL changes
        history_diff = data.attributes.get("history_diff", {})
        print(f"DEBUG: history_diff keys: {list(history_diff.keys())}")
        if "acl" not in history_diff:
            print("DEBUG: Returning early - no 'acl' in history_diff")
            return

        # Type narrowing for PyRight
        if not isinstance(data.shortname, str):
            logger.warning(
                "data.shortname is None and str is required at ticket_acl_email_notifier"
            )
            return

        try:
            # Get the old and new ACL values
            acl_diff = history_diff["acl"]
            old_acl = acl_diff.get("old", [])
            new_acl = acl_diff.get("new", [])

            # Handle case where old or new might be None or "null"
            if old_acl is None or old_acl == "null":
                old_acl = []
            if new_acl is None or new_acl == "null":
                new_acl = []

            # Convert to lists if they're not already
            if not isinstance(old_acl, list):
                old_acl = []
            if not isinstance(new_acl, list):
                new_acl = []

            # Extract user_shortnames from old and new ACL
            old_user_shortnames = set()
            new_user_shortnames = set()

            for acl_entry in old_acl:
                if isinstance(acl_entry, dict) and "user_shortname" in acl_entry:
                    old_user_shortnames.add(acl_entry["user_shortname"])
                elif hasattr(acl_entry, "user_shortname"):
                    old_user_shortnames.add(acl_entry.user_shortname)

            for acl_entry in new_acl:
                if isinstance(acl_entry, dict) and "user_shortname" in acl_entry:
                    new_user_shortnames.add(acl_entry["user_shortname"])
                elif hasattr(acl_entry, "user_shortname"):
                    new_user_shortnames.add(acl_entry.user_shortname)

            # Find newly added users
            newly_added_users = new_user_shortnames - old_user_shortnames
            print(f"DEBUG: old_user_shortnames={old_user_shortnames}, new_user_shortnames={new_user_shortnames}, newly_added={newly_added_users}")

            if not newly_added_users:
                print("DEBUG: Returning early - no newly added users")
                return

            # Load the ticket to get its shortname for the email message
            ticket = await db.load(
                space_name=data.space_name,
                subpath=data.subpath,
                shortname=data.shortname,
                class_type=Content,
                user_shortname=data.user_shortname,
            )

            # Send email to each newly added user
            for user_shortname in newly_added_users:
                try:
                    # Load user to get email address
                    user = await db.load(
                        space_name=settings.management_space,
                        subpath=settings.users_subpath,
                        shortname=user_shortname,
                        class_type=User,
                        user_shortname=data.user_shortname,
                    )

                    # Check if user has an email address
                    if not user.email:
                        logger.warning(
                            f"User {user_shortname} does not have an email address, skipping email notification"
                        )
                        continue

                    # Prepare email message
                    message = f"<p>Your action is needed for request {ticket.shortname}</p>"
                    subject = "Action Required for Request"

                    # Send email using SMTP
                    from_address = _plugin_smtp_config.get("from_address", settings.email_sender)
                    from_name = _plugin_smtp_config.get("from_name", "")
                    await send_email_smtp(
                        from_address=from_address,
                        to_address=user.email,
                        message=message,
                        subject=subject,
                        from_name=from_name,
                    )

                except Exception as e:
                    logger.error(
                        f"Error sending email notification to user {user_shortname} for ticket {ticket.shortname}: {e}"
                    )
            print("heyy i ran 3 ------------------------------")
        except Exception as e:
            logger.error(
                f"Error in ticket_acl_email_notifier plugin for ticket {data.shortname}: {e}"
            )


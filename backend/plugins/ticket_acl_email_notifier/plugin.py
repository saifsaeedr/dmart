from sys import modules as sys_modules
from models.core import Content, PluginBase, Event, ActionType, Ticket, User
from models.enums import ResourceType
from utils.helpers import camel_case
from utils.settings import settings
from api.user.service import send_email
from data_adapters.adapter import data_adapter as db
from fastapi.logger import logger


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

                    # Send email
                    success = await send_email(
                        from_address=settings.email_sender,
                        to_address=user.email,
                        message=message,
                        subject=subject,
                        send_email_api=settings.send_email_api,
                    )

                    if success:
                        logger.info(
                            f"Successfully sent ACL notification email to {user.email} for ticket {ticket.shortname}"
                        )
                    else:
                        logger.warning(
                            f"Failed to send ACL notification email to {user.email} for ticket {ticket.shortname}"
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


import asyncio
import csv
import datetime
import logging

import aiohttp
import discord
import isodate
import pytz
import tortoise.exceptions
from discord import app_commands
from discord.ext import commands, tasks
from slugify import slugify

import config
from alttprbot import models
from alttprbot.util import asynctournament, triforce_text
from alttprbot_api.util import checks
from alttprbot.alttprgen import generator

RACETIME_URL = config.RACETIME_URL
APP_URL = config.APP_URL

YES_NO_CHOICE = [
    app_commands.Choice(name="Yes", value=True),
    app_commands.Choice(name="No", value=False),
]


class AsyncTournamentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start new async run", style=discord.ButtonStyle.green, emoji="🏁",
                       custom_id="sahasrahbot:new_async_race")
    async def new_async_race(self, interaction: discord.Interaction, button: discord.ui.Button):
        async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
        await async_tournament.fetch_related('permalink_pools')

        if async_tournament is None:
            await interaction.response.send_message("This channel is not configured for async tournaments.",
                                                    ephemeral=True)
            return

        if async_tournament.active is False:
            await interaction.response.send_message("This tournament is not currently active.", ephemeral=True)
            return

        # check discord account age, this should also be configurable in the future
        # the age of the account must be at least 7 days older than the tournament start date
        if interaction.user.created_at > (async_tournament.created - datetime.timedelta(days=7)):
            await async_tournament.fetch_related('whitelist', 'whitelist__user')
            if interaction.user.id not in [w.user.discord_user_id for w in async_tournament.whitelist]:
                await interaction.response.send_message(
                    "Your Discord account is too new to participate in this tournament.  Please contact a tournament administrator for manual verification and whitelisting.",
                    ephemeral=True)
                return

        # this should be configurable in the future
        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)
        if user.rtgg_id is None:
            await interaction.response.send_message(
                f"You must link your RaceTime.gg account to SahasrahBot before you can participate in an async tournament.\n\nPlease visit <{APP_URL}/racetime/verification/initiate> to link your RaceTime account.",
                ephemeral=True)
            return

        async_history = await models.AsyncTournamentRace.filter(user=user,
                                                                tournament=async_tournament).prefetch_related(
            'permalink__pool')
        played_seeds = [a.permalink for a in async_history if a.reattempted is False]

        # get the pools of the played seeds
        played_pools = [a.pool for a in played_seeds]

        # get a count of each pool played
        played_pool_counts = {}
        for pool in played_pools:
            if pool in played_pool_counts:
                played_pool_counts[pool] += 1
            else:
                played_pool_counts[pool] = 1

        # get the pools that have not been played twice
        available_pools = [a for a in async_tournament.permalink_pools if a not in played_pool_counts or played_pool_counts[a] < async_tournament.runs_per_pool]

        if available_pools is None or len(available_pools) == 0:
            await interaction.response.send_message("You have already played all available pools for this tournament.",
                                                    ephemeral=True)
            return

        await interaction.response.send_message(
            "You must start your race within 10 minutes of clicking this button.\nFailure to do so will result in a forfeit.\n\n**Please be absolutely certain you're ready to begin.**\n\nThis dialogue box will expire in 60 seconds.  Dismiss this message if you performed this action in error.",
            view=AsyncTournamentRaceViewConfirmNewRace(available_pools=available_pools), ephemeral=True)

    # @discord.ui.button(label="Re-attempt", style=discord.ButtonStyle.blurple, emoji="↩️",
    #                    custom_id="sahasrahbot:async_reattempt")
    # async def async_reattempt(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
    #     await async_tournament.fetch_related('permalink_pools')

    #     if async_tournament is None:
    #         await interaction.response.send_message("This channel is not configured for async tournaments.",
    #                                                 ephemeral=True)
    #         return

    #     if async_tournament.active is False:
    #         await interaction.response.send_message("This tournament is not currently active.", ephemeral=True)
    #         return

    #     if async_tournament.allowed_reattempts is None or async_tournament.allowed_reattempts == 0:
    #         await interaction.response.send_message("This tournament does not allow re-attempts.", ephemeral=True)
    #         return

    #     user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)

    #     async_history = await models.AsyncTournamentRace.filter(user=user,
    #                                                             tournament=async_tournament).prefetch_related(
    #         'permalink__pool')
    #     played_seeds = [a.permalink for a in async_history if a.reattempted is False]
    #     if played_seeds is None or len(played_seeds) == 0:
    #         await interaction.response.send_message("You have not yet played any seeds for this tournament.",
    #                                                 ephemeral=True)
    #         return

    #     reattempts = [a for a in async_history if a.reattempted is True]

    #     available_reattempts = async_tournament.allowed_reattempts - len(reattempts)

    #     if available_reattempts < 1:
    #         await interaction.response.send_message(
    #             f"You have already used all of your re-attempts for this tournament.", ephemeral=True)
    #         return

    #     await interaction.response.send_message(
    #         f"Please choose a pool to reattempt.  You have **{available_reattempts}** re-attempts remaining.",
    #         view=AsyncTournamentRaceViewConfirmReattempt(played_pools=played_pools), ephemeral=True)

#     @discord.ui.button(label="View your history", style=discord.ButtonStyle.grey, emoji="📜",
#                        custom_id="sahasrahbot:async_history")
#     async def async_history(self, interaction: discord.Interaction, button: discord.ui.Button):
#         await interaction.response.defer()
#         async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)

#         if async_tournament is None:
#             await interaction.followup.send("This channel is not configured for async tournaments.", ephemeral=True)
#             return

#         pools = await models.AsyncTournamentPermalinkPool.filter(
#             tournament=async_tournament
#         )
#         description = f"""Disclaimer: Score calculation occurs at a fixed interval (hourly).
# If your score for a finished race is \"0\" or \"not calculated\", it may take up to an hour for the score to be calculated and displayed here.

# This number may also change as more players play the seed, as the score is calculated based on the par time, which is the top 5 runs.  This calculation also occurs hourly.

# Your finish times and scores should be kept private until the end of the qualifier period.

# If your run has a review status of "Rejected", this just means your run is undergoing secondary review.  If there's an issue with the run, we will contact you.
# A review status of "Accepted" indicates that all data adjustments have been performed.  "Pending" indicates that your run is still awaiting review.
# """
#         embed = discord.Embed(
#             title="Async Tournament History",
#             description=description,
#             color=discord.Color.blurple()
#         )

#         for pool in pools:
#             race = await models.AsyncTournamentRace.get_or_none(
#                 user__discord_user_id=interaction.user.id,
#                 tournament=async_tournament,
#                 permalink__pool=pool,
#                 reattempted=False
#             )
#             if race is None:
#                 status = "Not yet played"
#                 elapsed_time = "N/A"
#                 score = "N/A"
#                 review_status = "N/A"
#             else:
#                 status = race.status_formatted
#                 elapsed_time = race.elapsed_time_formatted
#                 score = race.score_formatted
#                 review_status = race.review_status_formatted

#             embed.add_field(
#                 name=pool.name,
#                 value=f"**Status:** {status}\n**Finish Time:** {elapsed_time}\n**Score:** {score}\n**Review Status:** {review_status}",
#                 inline=False
#             )

#         reattempts = await models.AsyncTournamentRace.filter(
#             user__discord_user_id=interaction.user.id,
#             tournament=async_tournament,
#             reattempted=True
#         ).prefetch_related('permalink__pool')
#         if reattempts:
#             embed.add_field(
#                 name="Re-attempted Pools",
#                 value="\n".join([f"{r.permalink.pool.name}" for r in reattempts])
#             )

#         await interaction.followup.send(embed=embed, ephemeral=True)


class AsyncTournamentViewConfirmCloseTournament(discord.ui.View):
    def __init__(self, view, interaction):
        super().__init__(timeout=60)
        self.original_view = view
        self.original_interaction = interaction

    @discord.ui.button(label="Yes, close this tournament", style=discord.ButtonStyle.red, emoji="🔒", row=2)
    async def async_confirm_close_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
        if async_tournament is None:
            await interaction.response.send_message(
                "This channel is not configured for async tournaments.  This should not have happened.", ephemeral=True)
            return

        if async_tournament.active is False:
            await interaction.response.send_message(
                "This tournament is not currently active.  This should not have happened.", ephemeral=True)
            return

        if interaction.user.id != async_tournament.owner_id:
            await interaction.response.send_message(
                "You are not the owner of this tournament.  This should not have happened.", ephemeral=True)
            return

        async_tournament.active = False
        await async_tournament.save()

        # for item in self.original_view.children:
        #     item.disabled = True
        # await self.original_interaction.followup.edit_message(message_id=self.original_interaction.message.id, view=self.original_view)

        await interaction.response.send_message("This tournament has been closed.")


class SelectPermalinkPool(discord.ui.Select):
    def __init__(self, pools):
        options = [discord.SelectOption(label=a.name) for a in list(pools)]
        super().__init__(placeholder="Select an async pool", max_values=1, min_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.pool = self.values[0]
        embed = discord.Embed(title="You have selected:", description=self.view.pool)
        for item in self.view.children:
            if item.type == discord.ComponentType.button:
                item.disabled = False
        await interaction.response.edit_message(embed=embed, view=self.view)

# This view is ephemeral


class AsyncTournamentRaceViewConfirmNewRace(discord.ui.View):
    def __init__(self, available_pools):
        super().__init__(timeout=60)
        self.pool = None
        self.add_item(SelectPermalinkPool(pools=available_pools))

    @discord.ui.button(label="I confirm, this is the point of no return!", style=discord.ButtonStyle.green, emoji="✅",
                       row=2, disabled=True)
    async def async_confirm_new_race(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Send a message to the thread with AsyncTournamentRaceViewReady
        # await interaction.response.send_message("NYI", ephemeral=True)
        async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
        if async_tournament is None:
            await interaction.response.send_message(
                "This channel is not configured for async tournaments.  This should not have happened.", ephemeral=True)
            return

        if async_tournament.active is False:
            await interaction.response.send_message(
                "This tournament is not currently active.  This should not have happened.", ephemeral=True)
            return

        if self.pool is None:
            await interaction.response.send_message("Please choose a pool.", ephemeral=True)
            return

        active_races_for_user = await async_tournament.races.filter(user__discord_user_id=interaction.user.id,
                                                                    status__in=["pending", "in_progress"])
        if active_races_for_user:
            await interaction.response.send_message(
                "You already have an active race.  If you believe this is in error, please contact a moderator.",
                ephemeral=True)
            return

        # Create a new private thread
        thread = await interaction.channel.create_thread(
            name=f"{slugify(interaction.user.name, lowercase=False, max_length=20)} - {self.pool}",
            type=discord.ChannelType.private_thread
        )

        pool = await async_tournament.permalink_pools.filter(
            name=self.pool).first()  # TODO: add a unique constraint on this

        # Double check that the player hasn't played from this pool already
        async_history = await models.AsyncTournamentRace.filter(user__discord_user_id=interaction.user.id,
                                                                tournament=async_tournament).prefetch_related(
            'permalink__pool')
        played_seeds = [a.permalink for a in async_history if a.reattempted is False]

        # check if pool has been played twice already
        num_played = len([a for a in played_seeds if a.pool == pool])
        if num_played >= async_tournament.runs_per_pool:
            await interaction.response.send_message("You have already played the maximum number of seeds from this pool.", ephemeral=True)
            return

        await pool.fetch_related("permalinks")

        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)
        permalink = await asynctournament.get_eligible_permalink_from_pool(pool, user)

        # Log the action
        await models.AsyncTournamentAuditLog.create(
            tournament=async_tournament,
            user=user,
            action="create_thread",
            details=f"Created thread {thread.id} for pool {pool.name}, permalink {permalink.url}"
        )

        # Write the race to the database
        async_tournament_race = await models.AsyncTournamentRace.create(
            tournament=async_tournament,
            thread_id=thread.id,
            user=user,
            thread_open_time=discord.utils.utcnow(),
            permalink=permalink,
        )

        # Invite the user to the thread
        await thread.add_user(interaction.user)

        # Create a post in that thread using AsyncTournamentRaceView
        embed = discord.Embed(title="Tournament Async Run")
        embed.add_field(name="Pool", value=self.pool, inline=False)
        embed.add_field(name="Permalink", value=permalink.url, inline=False)
        if permalink.notes:
            embed.add_field(name="Notes", value=permalink.notes, inline=False)
        embed.set_footer(text=f"Race ID: {async_tournament_race.id}")
        if async_tournament.customization == "gmpmt2023":
            msg = """
⚠️ Remember, **DO NOT STREAM YOURSELF PLAYING** in order to reduce the chances of another MT applicant getting spoiled on the details of these seeds.
When you are finished, take a screenshot of your In Game Time (IGT) and Collection Rate, which is provided at the end of the credits.
Though not required, you are welcome to record your run and share it with us by uploading it as Unlisted (not Private) on YouTube, with your name and the seed mode.

Good Luck, Have Fun!
            """
        else:
            msg = """
⚠️ Please read these reminders before clicking Ready! ⚠️

1. You must record your run and upload it to an **unlisted YouTube video**, or stream it to YouTube as an **unlisted** stream.
2. Do not make your video public until the qualifier stage has closed.
3. If you have any technical issues with Discord or this bot, **do not forfeit**.  Please continue to finish your run and then contact an admin after the run has concluded for further assistance.  We can fix the data in the bot if needed.
4. If you forfeit, even before you start your run, you will be required to use your reattempt to try again.  No exceptions.
5. Check if you are locally recording using **.mkv or .flv** file formats.  Do not use .mp4, as it can result in **corrupted video files** if OBS crashes.

**__DO NOT USE TWITCH.TV__**

Good luck and have fun!
            """
        await thread.send(content=msg, embed=embed, view=AsyncTournamentRaceViewReady())
        await interaction.response.edit_message(
            content=f"Successfully created {thread.mention}.  Please join that thread for more details.", view=None,
            embed=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌", row=2)
    async def async_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Successfully cancelled this request.  It will not be placed on your record and may be attempted at a later time.",
            view=None, embed=None)


# class AsyncTournamentRaceViewConfirmReattempt(discord.ui.View):
#     def __init__(self, played_seeds):
#         super().__init__(timeout=60)
#         self.pool = None
#         self.add_item(SelectPermalink(seeds=played_seeds))

#     @discord.ui.button(label="I confirm!  I will not be allowed to undo this decision.",
#                        style=discord.ButtonStyle.green, emoji="✅", row=2, disabled=True)
#     async def async_confirm_new_race(self, interaction: discord.Interaction, button: discord.ui.Button):
#         # Send a message to the thread with AsyncTournamentRaceViewReady
#         async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
#         if async_tournament is None:
#             await interaction.response.send_message(
#                 "This channel is not configured for async tournaments.  This should not have happened.", ephemeral=True)
#             return

#         if async_tournament.active is False:
#             await interaction.response.send_message(
#                 "This tournament is not currently active.  This should not have happened.", ephemeral=True)
#             return

#         if async_tournament.allowed_reattempts is None or async_tournament.allowed_reattempts == 0:
#             await interaction.response.send_message(
#                 "This tournament does not allow re-attempts.  This should not have happened.", ephemeral=True)
#             return

#         if self.pool is None:
#             await interaction.response.send_message("Please choose a pool.", ephemeral=True)
#             return

#         user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)

#         active_races_for_user = await async_tournament.races.filter(user=user, status__in=["pending", "in_progress"])
#         if active_races_for_user:
#             await interaction.response.send_message(
#                 "You already have an active race.  If you believe this is in error, please contact a moderator.",
#                 ephemeral=True)
#             return

#         previous_tournament_races_for_pool = await models.AsyncTournamentRace.filter(user=user,
#                                                                                      tournament=async_tournament,
#                                                                                      permalink__pool__name=self.pool).prefetch_related(
#             'permalink__pool')
#         for race in previous_tournament_races_for_pool:
#             await models.AsyncTournamentAuditLog.create(
#                 tournament=async_tournament,
#                 user=user,
#                 action="reattempt",
#                 details=f"Marked {race.id} as a re-attempt for pool {self.pool}"
#             )
#             race.reattempted = True
#             await race.save()

#         await interaction.response.edit_message(
#             content="Successfully marked your previous race in this pool as a re-attempt.  Please choose a new permalink.",
#             view=None, embed=None)

#     @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌", row=2)
#     async def async_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
#         await interaction.response.edit_message(
#             content="Successfully cancelled this request.  You have not used a re-attempt.", view=None, embed=None)


class AsyncTournamentRaceViewReady(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ready (start countdown)", style=discord.ButtonStyle.green, emoji="✅",
                       custom_id="sahasrahbot:async_ready")
    async def async_ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        tournament_race = await models.AsyncTournamentRace.get_or_none(
            thread_id=interaction.channel.id).prefetch_related('user')
        if tournament_race is None:
            await interaction.response.send_message(
                "This thread is not configured for async tournaments.  This should not have happened.", ephemeral=True)
            return

        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)

        if tournament_race.user.discord_user_id != interaction.user.id:
            await interaction.response.send_message("Only the runner of this race can start it.", ephemeral=True)
            return

        if tournament_race.status != "pending":
            await interaction.response.send_message("This race must be in the pending state to start it.",
                                                    ephemeral=True)

        await interaction.response.defer()

        await models.AsyncTournamentAuditLog.create(
            tournament_id=tournament_race.tournament_id,
            user=user,
            action="race_ready",
            details=f"{tournament_race.id} is marked as ready"
        )

        for child_item in self.children:
            child_item.disabled = True
        await interaction.followup.edit_message(message_id=interaction.message.id, view=self)

        tournament_race.status = "in_progress"
        await tournament_race.save()

        await models.AsyncTournamentAuditLog.create(
            tournament_id=tournament_race.tournament_id,
            user=user,
            action="race_countdown",
            details=f"{tournament_race.id} is starting a countdown"
        )

        for i in range(10, 0, -1):
            await interaction.channel.send(f"{i}...")
            await asyncio.sleep(1)
        await interaction.channel.send("**GO!**", view=AsyncTournamentRaceViewInProgress())
        start_time = discord.utils.utcnow()

        tournament_race.start_time = start_time
        await tournament_race.save()

        await models.AsyncTournamentAuditLog.create(
            tournament_id=tournament_race.tournament_id,
            user=user,
            action="race_started",
            details=f"{tournament_race.id} has started"
        )

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.red, emoji="🏳️",
                       custom_id="sahasrahbot:async_forfeit")
    async def async_forfeit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async_tournament_race = await models.AsyncTournamentRace.get_or_none(
            thread_id=interaction.channel.id).prefetch_related('user')
        if async_tournament_race.user.discord_user_id != interaction.user.id:
            await interaction.response.send_message("Only the runner may forfeit this race.", ephemeral=True)
            return

        if async_tournament_race.status not in ["pending", "in_progress"]:
            await interaction.response.send_message("The race must be pending or in progress to forfeit it.",
                                                    ephemeral=True)
            return

        await interaction.response.send_message(
            "Are you sure you wish to forfeit?  Think carefully, as this action **cannot be undone**.  This race will be scored as a **zero** on your record and you may not re-play the run.",
            view=AsyncTournamentRaceViewForfeit(view=self, interaction=interaction), ephemeral=True)


class AsyncTournamentRaceViewInProgress(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.green, emoji="✅", custom_id="sahasrahbot:async_finish")
    async def async_finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await finish_race(interaction)

        for child_item in self.children:
            child_item.disabled = True
        await interaction.followup.edit_message(message_id=interaction.message.id, view=self)

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.red, emoji="🏳️",
                       custom_id="sahasrahbot:async_forfeit2")
    async def async_forfeit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async_tournament_race = await models.AsyncTournamentRace.get_or_none(
            thread_id=interaction.channel.id).prefetch_related('user')
        if async_tournament_race.user.discord_user_id != interaction.user.id:
            await interaction.response.send_message("Only the runner may forfeit this race.", ephemeral=True)
            return

        if async_tournament_race.status not in ["pending", "in_progress"]:
            await interaction.response.send_message("The race must be pending or in progress to forfeit it.",
                                                    ephemeral=True)
            return

        await interaction.response.send_message(
            "Are you sure you wish to forfeit?  Think carefully, as this action **cannot be undone**.  This race will be scored as a **zero** on your record and you may not re-play the run.",
            view=AsyncTournamentRaceViewForfeit(view=self, interaction=interaction), ephemeral=True)

    @discord.ui.button(label="Get timer", style=discord.ButtonStyle.gray, emoji="⏱️",
                       custom_id="sahasrahbot:async_get_timer")
    async def async_get_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        race = await models.AsyncTournamentRace.get_or_none(thread_id=interaction.channel.id)
        if race.status in ["forfeit", "finished", "disqualified"]:
            await interaction.response.send_message("Race is already finished.", ephemeral=True)
            return

        await interaction.response.send_message(f"Timer: **{race.elapsed_time_formatted}**", ephemeral=True)


class AsyncTournamentRaceViewForfeit(discord.ui.View):
    def __init__(self, view, interaction):
        super().__init__(timeout=60)
        self.original_view = view
        self.original_interaction = interaction

    @discord.ui.button(label="Confirm Forfeit", style=discord.ButtonStyle.red, emoji="🏳️")
    async def async_confirm_forfeit(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Write forfeit to database
        # Disable buttons on this view
        async_tournament_race = await models.AsyncTournamentRace.get_or_none(
            thread_id=interaction.channel.id).prefetch_related('user')
        if async_tournament_race is None:
            await interaction.response.send_message("This race does not exist.  Please contact a moderator.",
                                                    ephemeral=True)
            return

        if async_tournament_race.user.discord_user_id != interaction.user.id:
            await interaction.response.send_message("Only the runner may forfeit this race.", ephemeral=True)
            return

        if async_tournament_race.status not in ["pending", "in_progress"]:
            await interaction.response.send_message("The race must be pending or in progress to forfeit it.",
                                                    ephemeral=True)
            return

        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)

        await models.AsyncTournamentAuditLog.create(
            tournament_id=async_tournament_race.tournament_id,
            user=user,
            action="runner_forfeit",
            details=f"{async_tournament_race.id} was forfeited by runner"
        )

        async_tournament_race.status = "forfeit"
        await async_tournament_race.save()
        await interaction.response.send_message(f"This run has been forfeited by {interaction.user.mention}.")
        for child_item in self.children:
            child_item.disabled = True
        await interaction.followup.edit_message(message_id=interaction.message.id, view=self)

        for child_item in self.original_view.children:
            child_item.disabled = True
        await self.original_interaction.followup.edit_message(message_id=self.original_interaction.message.id,
                                                              view=self.original_view)


# button to open a modal to submit vod link and runner notes
class AsyncTournamentPostRaceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Submit Run Information", style=discord.ButtonStyle.green, emoji="🗒️",
                       custom_id="sahasrahbot:async_submit_vod")
    async def async_submit_vod(self, interaction: discord.Interaction, button: discord.ui.Button):
        race = await models.AsyncTournamentRace.get_or_none(thread_id=interaction.channel.id).prefetch_related('user',
                                                                                                               'tournament')
        if race is None:
            await interaction.response.send_message(
                "This is not a race thread.  This should not have happened.  Please contact a moderator.")
            return

        if race.user.discord_user_id != interaction.user.id:
            await interaction.response.send_message("Only the player may submit a VoD.", ephemeral=True)

        if race.tournament.customization == "gmpmt2023":
            await interaction.response.send_modal(SubmitCollectIGTModal())
        else:
            await interaction.response.send_modal(SubmitVODModal())


class SubmitVODModal(discord.ui.Modal, title="Submit VOD and Notes"):
    runner_vod_url = discord.ui.TextInput(label="VOD Link", placeholder="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                          row=0)
    runner_notes = discord.ui.TextInput(
        label="Runner Notes",
        placeholder="Please do not include HTML, though markdown is supported.",
        style=discord.TextStyle.long,
        required=False,
        row=1
    )

    async def on_submit(self, interaction: discord.Interaction):
        # write vod link and runner notes to database
        # close modal
        async_tournament_race = await models.AsyncTournamentRace.get_or_none(thread_id=interaction.channel.id)
        if async_tournament_race is None:
            await interaction.response.send_message(
                "This race does not exist.  This should not have happened.  Please contact a moderator.")
            return

        async_tournament_race.runner_vod_url = self.runner_vod_url.value
        async_tournament_race.runner_notes = self.runner_notes.value
        await async_tournament_race.save()

        await interaction.response.send_message(
            f"VOD link and runner notes saved.\n\n**URL:**\n{self.runner_vod_url.value}\n\n**Notes:**\n{self.runner_notes.value}")


class SubmitCollectIGTModal(discord.ui.Modal, title="Submit Collection Rate and IGT"):
    run_collection_rate = discord.ui.TextInput(label="Collection Rate", placeholder="169", row=0)
    run_igt = discord.ui.TextInput(label="IGT (hh:mm:ss.mm)", placeholder="1:23:45.67", row=1)

    async def on_submit(self, interaction: discord.Interaction):
        async_tournament_race = await models.AsyncTournamentRace.get_or_none(thread_id=interaction.channel.id)
        if async_tournament_race is None:
            await interaction.response.send_message(
                "This race does not exist.  This should not have happened.  Please contact a moderator.")
            return

        async_tournament_race.run_collection_rate = self.run_collection_rate.value
        async_tournament_race.run_igt = self.run_igt.value
        await async_tournament_race.save()

        await interaction.response.send_message(
            f"Collection rate and IGT saved.\n\n**Collection Rate: **\n{self.run_collection_rate.value}\n\n**IGT: **\n{self.run_igt.value}\nThank you for your submission. Now is the time to share a screenshot of your end card with the In Game Time and Collection rate to this bot. If you recorded your run, share that link here as well. We will notify you if any additional information is needed before the tournament starts.\nThank you very much for your interest in participating!")


class AsyncTournament(commands.GroupCog, name="async"):
    def __init__(self, bot):
        self.bot: commands.Bot = bot
        self.timeout_warning_task.start()
        self.timeout_in_progress_races_task.start()
        self.score_calculation_task.start()
        self.persistent_views_added = False

    @tasks.loop(seconds=60, reconnect=True)
    async def timeout_warning_task(self):
        try:
            pending_races = await models.AsyncTournamentRace.filter(status="pending",
                                                                    thread_id__isnull=False).prefetch_related('user')
            for pending_race in pending_races:
                # make these configurable
                if pending_race.thread_timeout_time is None:
                    pending_race.thread_timeout_time = pending_race.thread_open_time + datetime.timedelta(minutes=20)
                    await pending_race.save()

                warning_time = pending_race.thread_timeout_time - datetime.timedelta(minutes=10)
                forfeit_time = pending_race.thread_timeout_time

                thread = self.bot.get_channel(pending_race.thread_id)
                if thread is None:
                    logging.warning("Cannot access thread for pending race %s.  This should not have happened.",
                                    pending_race.id)
                    continue

                if warning_time < discord.utils.utcnow() < forfeit_time:
                    await thread.send(
                        f"<@{pending_race.user.discord_user_id}>, your race will be permanently forfeit on {discord.utils.format_dt(forfeit_time, 'f')} ({discord.utils.format_dt(forfeit_time, 'R')}) if you do not start it by then.  Please start your run as soon as possible.  Please ping the @Admins if you require more time.",
                        allowed_mentions=discord.AllowedMentions(users=True))

                if forfeit_time < discord.utils.utcnow():
                    await thread.send(
                        f"<@{pending_race.user.discord_user_id}>, the grace period for the start of this run has elapsed.  This run has been forfeit.  Please contact the @Admins if you believe this was in error.",
                        allowed_mentions=discord.AllowedMentions(users=True))
                    pending_race.status = "forfeit"
                    await pending_race.save()
                    await models.AsyncTournamentAuditLog.create(
                        tournament_id=pending_race.tournament_id,
                        action="timeout_forfeit",
                        details=f"{pending_race.id} was automatically forfeited by System due to timeout",
                    )
        except Exception:
            logging.exception("Exception in timeout_warning_task")

    @tasks.loop(seconds=60, reconnect=True)
    async def timeout_in_progress_races_task(self):
        try:
            races = await models.AsyncTournamentRace.filter(status="in_progress",
                                                            thread_id__isnull=False).prefetch_related('user')
            for race in races:
                if race.start_time is None:
                    continue

                if race.start_time + datetime.timedelta(hours=12) > discord.utils.utcnow():
                    # this race is still in progress and has not timed out
                    continue

                thread = self.bot.get_channel(race.thread_id)
                if thread is None:
                    logging.warning("Cannot access thread for pending race %s.  This should not have happened.",
                                    race.id)
                    continue

                await thread.send(
                    f"<@{race.user.discord_user_id}>, this race has exceeded 12 hours.  This run has been forfeit.  Please contact the @Admins if you believe this was in error.",
                    allowed_mentions=discord.AllowedMentions(users=True))
                race.status = "forfeit"
                await race.save()
                await models.AsyncTournamentAuditLog.create(
                    tournament_id=race.tournament_id,
                    action="timeout_forfeit",
                    details=f"in progress race \"{race.id}\" was automatically forfeited by System due to timeout",
                )
        except Exception:
            logging.exception("Exception in timeout_in_progress_races_task")

    @tasks.loop(hours=1, reconnect=True)
    async def score_calculation_task(self):
        try:
            tournaments = await models.AsyncTournament.filter(active=True)
            for tournament in tournaments:
                logging.info("Calculating scores for tournament %s", tournament.id)
                try:
                    await asynctournament.calculate_async_tournament(tournament)
                except Exception:
                    logging.exception("Exception in score_calculation_task for tournament %s", tournament.id)
                logging.info("Finished calculating scores for tournament %s", tournament.id)
        except Exception:
            logging.exception("Exception in score_calculation_task")

    @timeout_warning_task.before_loop
    async def before_timeout_warning_task(self):
        await self.bot.wait_until_ready()

    @timeout_in_progress_races_task.before_loop
    async def before_timeout_in_progress_races_task(self):
        await self.bot.wait_until_ready()

    @score_calculation_task.before_loop
    async def before_score_calculation_task(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.persistent_views_added:
            self.bot.add_view(AsyncTournamentView())
            self.bot.add_view(AsyncTournamentRaceViewReady())
            self.bot.add_view(AsyncTournamentRaceViewInProgress())
            self.bot.add_view(AsyncTournamentPostRaceView())
            self.persistent_views_added = True

    @app_commands.command(name="create", description="Create an async tournament.  This command is only available to Synack.")
    async def create(self, interaction: discord.Interaction, name: str, permalinks: str,
                     report_channel: discord.TextChannel = None):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only Synack may create an async tournament at this time.",
                                                    ephemeral=True)
            return

        await interaction.response.defer()
        embed = discord.Embed(title=name)
        try:
            async_tournament = await models.AsyncTournament.create(
                name=name,
                report_channel_id=report_channel.id if report_channel else None,
                active=True,
                guild_id=interaction.guild.id,
                channel_id=interaction.channel.id,
                owner_id=interaction.user.id
            )
        except tortoise.exceptions.IntegrityError:
            await interaction.followup.send(
                "An async tournament is already associated with this channel.  Please create a new channel for the tournament or contact Synack for further assistance.",
                ephemeral=True)
            return

        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)

        await models.AsyncTournamentAuditLog.create(
            tournament=async_tournament,
            user=user,
            action="create",
            details=f"{name} ({async_tournament.id}) created"
        )

        for row in permalinks.split(';'):
            pool_name, preset, num = row.split(',')
            pool = await models.AsyncTournamentPermalinkPool.create(
                tournament=async_tournament,
                name=pool_name,
                preset=preset,
            )

            for _ in range(int(num)):
                seed = await generator.ALTTPRPreset(preset=preset).generate(
                    tournament=True,
                    allow_quickswap=True
                )
                await models.AsyncTournamentPermalink.create(
                    pool=pool,
                    url=seed.url,
                    notes='/'.join(seed.code),
                    live_race=False,
                )

        embed = create_tournament_embed(async_tournament)
        await interaction.followup.send(embed=embed, view=AsyncTournamentView())

    @app_commands.command(name="addseed", description="Add a seed to an async tournament.  This command is only available to Synack.")
    async def add_seed(self, interaction: discord.Interaction, pool_name: str, preset: str, num: int=1):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only Synack may create an async tournament at this time.",
                                                    ephemeral=True)
            return

        async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel.id)
        if async_tournament is None:
            await interaction.response.send_message(
                "This channel is not configured for async tournaments.  Please create a new tournament.",
                ephemeral=True)
            return

        await interaction.response.defer()
        pool = await models.AsyncTournamentPermalinkPool.get(
            tournament=async_tournament,
            name=pool_name,
        )

        for _ in range(num):
            seed = await generator.ALTTPRPreset(preset=preset).generate(
                tournament=True,
                allow_quickswap=True
            )
            await models.AsyncTournamentPermalink.create(
                pool=pool,
                url=seed.url,
                notes='/'.join(seed.code),
                live_race=False,
            )

        await interaction.followup.send(f"Added {num} seeds to pool {pool_name}.")

    @app_commands.command(name="extendtimeout", description="Extend the timeout of this tournament run")
    async def extend_timeout(self, interaction: discord.Interaction, minutes: int):
        # TODO: replace this with a lookup on the config table for authorized users
        # if not await self.bot.is_owner(interaction.user):
        #     await interaction.response.send_message("Only Synack may create an async tournament at this time.", ephemeral=True)
        #     return

        async_tournament_race = await models.AsyncTournamentRace.get_or_none(thread_id=interaction.channel.id)
        if not async_tournament_race:
            await interaction.response.send_message("This channel is not an async tournament thread.", ephemeral=True)
            return

        if async_tournament_race.status != "pending":
            await interaction.response.send_message("This race is not pending.  It cannot be extended.", ephemeral=True)
            return

        await async_tournament_race.fetch_related("tournament")

        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)
        authorized = await checks.is_async_tournament_user(user, async_tournament_race.tournament, ['admin', 'mod'])
        if not authorized:
            await interaction.response.send_message(
                "You are not authorized to extend the timeout for this tournament race.", ephemeral=True)
            return

        if async_tournament_race.thread_timeout_time is None:
            thread_timeout_time = async_tournament_race.thread_open_time + datetime.timedelta(
                minutes=20) + datetime.timedelta(minutes=minutes)
        else:
            thread_timeout_time = async_tournament_race.thread_timeout_time + datetime.timedelta(minutes=minutes)

        async_tournament_race.thread_timeout_time = thread_timeout_time
        await async_tournament_race.save()

        await models.AsyncTournamentAuditLog.create(
            tournament_id=async_tournament_race.tournament_id,
            user=user,
            action="extend_timeout",
            details=f"{async_tournament_race.id} extended by {minutes} minutes"
        )

        await interaction.response.send_message(
            f"Timeout extended to {discord.utils.format_dt(thread_timeout_time, 'f')} ({discord.utils.format_dt(thread_timeout_time, 'R')}).")

    @app_commands.command(name="repost", description="Repost the tournament embed")
    async def repost(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only Synack may create an async tournament at this time.",
                                                    ephemeral=True)
            return

        await interaction.response.defer()
        async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel.id)
        if async_tournament is None:
            await interaction.followup.send(
                "This channel is not configured for async tournaments.  Please create a new tournament.",
                ephemeral=True)
            return

        embed = create_tournament_embed(async_tournament)
        await interaction.followup.send(embed=embed, view=AsyncTournamentView())

    @app_commands.command(name="done", description="Finish the current race.")
    async def done(self, interaction: discord.Interaction):
        await finish_race(interaction)

    @app_commands.command(name="permissions", description="Configure permissions for this tournament")
    async def permissions(self, interaction: discord.Interaction, permission: str, role: discord.Role = None,
                          user: discord.User = None):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only Synack may create an async tournament at this time.",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel.id)
        if async_tournament is None:
            await interaction.followup.send(
                "This channel is not configured for async tournaments.  Please create a new tournament.",
                ephemeral=True)
            return

        if permission not in ["admin", "mod"]:
            await interaction.followup.send("Invalid permission.  Valid permissions are: admin, mod", ephemeral=True)
            return

        if role is None and user is None:
            await interaction.followup.send("Please specify a role or user.", ephemeral=True)
            return

        if role is not None and user is not None:
            await interaction.followup.send("Please specify a role OR a user, not both.", ephemeral=True)
            return

        if role is not None:
            if interaction.guild.chunked is False:
                await interaction.guild.chunk()

            for member in role.members:
                dbuser, _ = await models.Users.get_or_create(discord_user_id=member.id,
                                                             defaults={"display_name": member.name})
                async_tournament_permission = await models.AsyncTournamentPermissions.get_or_none(
                    tournament=async_tournament,
                    user=dbuser,
                    role=permission
                )
                if async_tournament_permission is None:
                    await models.AsyncTournamentPermissions.create(
                        tournament=async_tournament,
                        user=dbuser,
                        role=permission
                    )
            await interaction.followup.send(
                f"Users in role {role.name} ({role.id}) has been granted {permission} permissions.", ephemeral=True)

        elif user is not None:
            dbuser, _ = await models.Users.get_or_create(discord_user_id=user.id, defaults={"display_name": user.name})
            async_tournament_permission = await models.AsyncTournamentPermissions.get_or_none(
                tournament=async_tournament,
                user=dbuser,
                role=permission
            )
            if async_tournament_permission is None:
                await models.AsyncTournamentPermissions.create(
                    tournament=async_tournament,
                    user=dbuser,
                    role=permission
                )

            await interaction.followup.send(f"{user.name} ({user.id}) has been granted {permission} permissions.",
                                            ephemeral=True)

    # TODO: write autocomplete racetime_slug
    @app_commands.command(name="live_race_record", description="Used record the results of a live qualifier race.")
    async def live_race_record(self, interaction: discord.Interaction, racetime_slug: str, force: bool = False):
        async_live_race = await models.AsyncTournamentLiveRace.get_or_none(racetime_slug=racetime_slug)
        if async_live_race is None:
            await interaction.response.send_message("That episode ID is not a async tournament live race.",
                                                    ephemeral=True)
            return

        if not async_live_race.status == "in_progress" and not force:
            await interaction.response.send_message("This race is not currently in progress.", ephemeral=True)
            return

        await async_live_race.fetch_related("tournament")

        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)
        authorized = await checks.is_async_tournament_user(user, async_live_race.tournament, ['admin', 'mod'])
        if not authorized:
            await interaction.response.send_message("You are not authorized to record this live race.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{RACETIME_URL}/{async_live_race.racetime_slug}/data") as resp:
                if resp.status != 200:
                    await interaction.followup.send(
                        f"Error fetching {async_live_race.racetime_slug}. Please try again.", ephemeral=True)
                    return

                data = await resp.json()

        # if not data["status"]["value"] == "finished":
        #     await interaction.followup.send(f"{RACETIME_URL}/{async_live_race.racetime_slug} is not finished.", ephemeral=True)
        #     return

        warnings = []
        for entrant in data["entrants"]:
            entrant_id = entrant["user"]["id"]
            entrant_name = entrant["user"]["name"]
            logging.info(f"Processing entrant {entrant_name} ({entrant_id})...")

            race_user = await models.Users.get_or_none(rtgg_id=entrant_id)

            if race_user is None:
                logging.warning(
                    f"Entrant {entrant_name} ({entrant_id}) is not in the user table.  This should not have happened.")
                warnings.append(
                    f"Entrant {entrant_name} ({entrant_id}) is not in the user table.  This should not have happened.")
                continue

            race = await models.AsyncTournamentRace.get_or_none(
                live_race=async_live_race,
                user=race_user,
            )

            if race is None:
                # this runner wasn't eligible, so we're going to skip them
                continue

            if entrant['status']['value'] == 'done':
                race.end_time = isodate.parse_datetime(entrant["finished_at"]).astimezone(pytz.utc)
                race.status = "finished"
            elif entrant['status']['value'] == 'dnf':
                race.status = "forfeit"
            elif entrant['status']['value'] == 'dq':
                # record the time they were disqualified at for historical purposes
                race.end_time = isodate.parse_datetime(entrant["finished_at"]).astimezone(pytz.utc)
                race.status = "disqualified"
            else:
                warnings.append(
                    f"{entrant['user']['name']} is not finished, forfeited, or disqualified.  THis runner is likely still in progress, and this race will need to be recorded again.")

            await race.save()

        races_still_in_progress = await models.AsyncTournamentRace.filter(
            live_race=async_live_race,
            status="in_progress"
        ).count()

        if races_still_in_progress:
            warnings.append(
                f"**There are still {races_still_in_progress} still in progress for this live race**, even after recording.  You'll need to record this race again when they finish.")
        else:
            async_live_race.status = "finished"
            await async_live_race.save(update_fields=["status"])

        if warnings:
            await interaction.followup.send(
                "There were some warnings when recording this race.  Please report this to Synack so he can investigate further:\n" + "\n".join(
                    warnings), ephemeral=True)
        else:
            await interaction.followup.send("The recording of this race finished without any warnings!", ephemeral=True)

    if config.DEBUG:
        @app_commands.command(name="test", description="Populate tournament with dummy data.")
        async def test(self, interaction: discord.Interaction, participant_count: int = 1):
            if not await self.bot.is_owner(interaction.user):
                await interaction.response.send_message("Only Synack may perform this action.", ephemeral=True)
                return

            tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
            if tournament is None:
                await interaction.response.send_message("This channel is not configured for async tournaments.",
                                                        ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            await asynctournament.populate_test_data(tournament=tournament, participant_count=participant_count)

            await interaction.followup.send("Done!", ephemeral=True)

    @app_commands.command(name="calculate_scores", description="Calculate the scores for a async tournament.")
    async def calculate_scores(self, interaction: discord.Interaction, only_approved: bool = False):
        tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
        if tournament is None:
            await interaction.response.send_message("This channel is not configured for async tournaments.",
                                                    ephemeral=True)
            return

        # if tournament.active is False:
        #     await interaction.response.send_message("This tournament is not active.", ephemeral=True)
        #     return

        user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)
        authorized = await checks.is_async_tournament_user(user, tournament, ['admin'])
        if not authorized:
            await interaction.response.send_message("You are not authorized to perform a score recalculation.",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        await asynctournament.calculate_async_tournament(tournament, only_approved=only_approved)

        await interaction.followup.send("Done!", ephemeral=True)

    @live_race_record.autocomplete("racetime_slug")
    async def autocomplete_racetime_slug(self, interaction: discord.Interaction, current: str):
        result = await models.AsyncTournamentLiveRace.filter(racetime_slug__icontains=current,
                                                             status="in_progress").values("racetime_slug")
        return [app_commands.Choice(name=r["racetime_slug"], value=r["racetime_slug"]) for r in result]

    @app_commands.command(name="close", description="Close a async tournament.")
    async def close(self, interaction: discord.Interaction):
        async_tournament = await models.AsyncTournament.get_or_none(channel_id=interaction.channel_id)
        if async_tournament is None:
            await interaction.response.send_message("This channel is not configured for async tournaments.",
                                                    ephemeral=True)
            return

        if async_tournament.active is False:
            await interaction.response.send_message("This tournament is not currently active.", ephemeral=True)
            return

        if interaction.user.id != async_tournament.owner_id:
            await interaction.response.send_message("You are not the owner of this tournament.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Are you sure you want to close this tournament?\n\nThis action cannot be undone.",
            view=AsyncTournamentViewConfirmCloseTournament(view=self, interaction=interaction), ephemeral=True)

    @app_commands.command(name="update_run", description="Fixes a run that was recorded incorrectly.")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="finished", value="finished"),
            app_commands.Choice(name="forfeit", value="forfeit"),
            app_commands.Choice(name="disqualified", value="disqualified"),
        ]
    )
    async def update_run(self, interaction: discord.Interaction, status: str = None, elapsed_time: str = None,
                         vod_url: str = None):
        race = await models.AsyncTournamentRace.get_or_none(thread_id=interaction.channel.id).prefetch_related(
            "tournament")
        if race is None:
            await interaction.response.send_message("There is no async run in this thread.", ephemeral=True)
            return

        if not race.tournament.owner_id == interaction.user.id:
            await interaction.response.send_message("Only the owner of this tournament may update runs.",
                                                    ephemeral=True)
            return

        msg = f"{interaction.user.name} administratively updated this run:\n\n"

        if status:
            race.status = status
            msg += f"Status: {status}\n"

        if elapsed_time:
            time_obj = datetime.datetime.strptime(elapsed_time, "%H:%M:%S")
            timedelta_obj = datetime.timedelta(
                hours=time_obj.hour,
                minutes=time_obj.minute,
                seconds=time_obj.second
            )
            race.end_time = discord.utils.utcnow()
            race.start_time = race.end_time - timedelta_obj
            msg += f"Elapsed time: {elapsed_time}\n"

        if vod_url:
            race.runner_vod_url = vod_url
            msg += f"VOD URL: {vod_url}\n"

        if isinstance(race.runner_notes, str):
            race.runner_notes += f"\n\n{msg}"
        else:
            race.runner_notes = msg

        await race.save(update_fields=["status", "start_time", "end_time", "runner_vod_url", "runner_notes"])

        await interaction.response.send_message(msg)


def create_tournament_embed(async_tournament: models.AsyncTournament):
    embed = discord.Embed(title=async_tournament.name)
    embed.add_field(name="Owner", value=f"<@{async_tournament.owner_id}>", inline=False)
    embed.set_footer(text=f"ID: {async_tournament.id}")
    return embed


async def finish_race(interaction: discord.Interaction):
    race = await models.AsyncTournamentRace.get_or_none(thread_id=interaction.channel.id).prefetch_related('user',
                                                                                                           'tournament')
    if race is None:
        await interaction.response.send_message("This channel/thread is not an async race room.", ephemeral=True)
        return

    if race.user.discord_user_id != interaction.user.id:
        await interaction.response.send_message("Only the player of this race may finish it.", ephemeral=True)
        return

    if race.status != "in_progress":
        await interaction.response.send_message("You may only finish a race that's in progress.", ephemeral=True)
        return

    user, _ = await models.Users.get_or_create(discord_user_id=interaction.user.id)

    await models.AsyncTournamentAuditLog.create(
        tournament_id=race.tournament_id,
        user=user,
        action="race_finish",
        details=f"{race.id} has finished"
    )

    race.end_time = discord.utils.utcnow()
    race.status = "finished"
    await race.save()

    if race.tournament.customization == "gmpmt2023":
        await interaction.response.send_message(f"""
Your finish time of **{race.elapsed_time_formatted}** has been recorded. Thank you for playing!

Don't forget to submit your Collection Rate and In-game Time using the button below, and post in this channel a screenshot of your end card.
            """, view=AsyncTournamentPostRaceView())
    else:
        await interaction.response.send_message(
            f"Your finish time of **{race.elapsed_time_formatted}** has been recorded.  Thank you for playing!\n\nDon't forget to submit a VoD of your run using the button below!",
            view=AsyncTournamentPostRaceView())


def elapsed_time_hhmmss(elapsed: datetime.timedelta):
    hours, remainder = divmod(elapsed.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


async def setup(bot: commands.Bot):
    await bot.add_cog(AsyncTournament(bot))

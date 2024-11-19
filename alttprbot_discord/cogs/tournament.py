import datetime
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
# from alttprbot_discord.util import alttpr_discord
from alttprbot import models
from alttprbot import tournaments
from alttprbot.tournament import core, alttpr
from alttprbot.util import speedgaming

# TODO: use asyncio.semaphore() to limit the number of concurrent tasks

MAIN_TOURNAMENT_SERVERS = list(map(int, config.MAIN_TOURNAMENT_SERVERS.split(',')))
CC_TOURNAMENT_SERVERS = list(map(int, config.CC_TOURNAMENT_SERVERS.split(',')))
CC_TOURNAMENT_AUDIT_CHANNELS = int(config.CC_TOURNAMENT_AUDIT_CHANNELS)

MAIN_TOURNAMENT_ADMIN_ROLE_ID = 523276397679083520 if config.DEBUG else 334796844750209024
CC_TOURNAMENT_ADMIN_ROLE_ID = 523276397679083520 if config.DEBUG else 503724516854202370


class ChallengeCupDeleteHistoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Delete from Tournament History', style=discord.ButtonStyle.danger,
                       custom_id='sahabot:delete_history')
    async def delete_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild.chunked:
            await interaction.guild.chunk()

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You must be an administrator to delete history.", ephemeral=True)
            return

        embed = interaction.message.embeds[0]
        await interaction.user.send(f"Are you sure you want to delete the history of this race?\n\n{embed.title}",
                                    view=ChallengeCupDeleteHistoryConfirmationView(message=interaction.message))
        await interaction.response.send_message("Check your DMs.", ephemeral=True)


class ChallengeCupDeleteHistoryConfirmationView(discord.ui.View):
    def __init__(self, message: discord.Message):
        super().__init__(timeout=300)
        self.message = message

    @discord.ui.button(label='Confirm Delete History', style=discord.ButtonStyle.danger)
    async def delete_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_id = self.message.id

        await models.TournamentPresetHistory.filter(
            episode_id=message_id,
            event_slug='cc2023'
        ).delete()
        await self.message.add_reaction('🗑️')

        # disable buttons
        for child in self.children:
            child.disabled = True
        await self.message.edit(view=self)
        await interaction.response.send_message("History deleted.")


class Tournament(commands.Cog):
    def __init__(self, bot):
        self.bot: commands.Bot = bot
        self.create_races.start()
        self.record_races.start()
        self.week_races.start()
        self.find_races_with_bad_discord.start()
        self.persistent_views_added = False

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.persistent_views_added:
            self.bot.add_view(ChallengeCupDeleteHistoryView())
            self.persistent_views_added = True

    @tasks.loop(minutes=0.25 if config.DEBUG else 5, reconnect=True)
    async def create_races(self):
        try:
            logging.info("scanning SG schedule for tournament races to create")
            for event_slug, tournament_class in tournaments.TOURNAMENT_DATA.items():
                event_data: core.TournamentConfig = await tournament_class.get_config()
                try:
                    episodes = await speedgaming.get_upcoming_episodes_by_event(event_slug, hours_past=0.5,
                                                                                hours_future=event_data.hours_before_room_open)
                except Exception:
                    logging.exception("Encountered a problem when attempting to retrieve SG schedule.")
                    continue
                for episode in episodes:
                    logging.info(episode['id'])
                    await self.create_race_room(event_data, event_slug, episode)
        except Exception:
            logging.exception("An error occured while processing create_races.")
        logging.info('done')

    @tasks.loop(minutes=0.25 if config.DEBUG else 15, reconnect=True)
    async def week_races(self):
        try:
            logging.info('scanning for unsubmitted races')
            for event_slug, tournament_class in tournaments.TOURNAMENT_DATA.items():
                event_data: core.TournamentRace = await tournament_class.get_config()

                try:
                    episodes = await speedgaming.get_upcoming_episodes_by_event(event_slug, hours_past=0,
                                                                                hours_future=168)
                except Exception:
                    logging.exception("Encountered a problem when attempting to retrieve SG schedule.")
                    continue

                if event_data.submission_form:
                    for episode in episodes:
                        await self.send_race_form(event_data, event_slug, episode)

                if event_data.data.create_scheduled_events:
                    await self.update_scheduled_event(event_data, event_slug, episodes)

                if event_data.data.scheduling_needs_channel:
                    await self.update_scheduling_needs(event_data, episodes)
        except Exception:
            logging.exception("Encountered a problem when attempting to run week_races.")

    @tasks.loop(minutes=0.25 if config.DEBUG else 240, reconnect=True)
    async def find_races_with_bad_discord(self):
        logging.info('scanning for races with bad discord info')
        for event_slug, tournament_class in tournaments.TOURNAMENT_DATA.items():
            messages = await self.report_bad_player_discord(event_slug=event_slug)
            event_data: core.TournamentConfig = await tournament_class.get_config()

            if messages and event_data.audit_channel:
                await event_data.audit_channel.send("<@185198185990324225>\n\n" + "\n".join(messages))

    @tasks.loop(minutes=0.25 if config.DEBUG else 15, reconnect=True)
    async def record_races(self):
        try:
            logging.info("recording tournament races")
            await tournaments.race_recording_task()
            logging.info("done recording")
        except Exception:
            logging.exception("error recording")

    @create_races.before_loop
    async def before_create_races(self):
        logging.info('tournament create_races loop waiting...')
        await self.bot.wait_until_ready()

    @record_races.before_loop
    async def before_record_races(self):
        logging.info('tournament record_races loop waiting...')
        await self.bot.wait_until_ready()

    @week_races.before_loop
    async def before_find_unsubmitted_races(self):
        logging.info('tournament find_unsubmitted_races loop waiting...')
        await self.bot.wait_until_ready()

    @find_races_with_bad_discord.before_loop
    async def before_find_races_with_bad_discord(self):
        logging.info('tournament find_races_with_bad_discord loop waiting...')
        await self.bot.wait_until_ready()

    async def cog_check(self, ctx):  # pylint: disable=invalid-overridden-method
        if ctx.guild is None:
            return False

        if await ctx.guild.config_get('TournamentEnabled') == 'true':
            return True
        else:
            return False

    async def update_scheduled_event(self, event_data: core.TournamentRace, event_slug: str, episodes: dict):

        # remove dead events
        dead_events = await models.ScheduledEvents.filter(episode_id__not_in=[e['id'] for e in episodes],
                                                          event_slug=event_slug)
        for dead_event in dead_events:
            try:
                event: discord.ScheduledEvent = await event_data.guild.fetch_scheduled_event(
                    dead_event.scheduled_event_id)

                if event.status == discord.EventStatus.scheduled:
                    await event.delete()
            except discord.NotFound:
                continue
            await dead_event.delete()

        for episode in episodes:
            start_time = datetime.datetime.strptime(episode['when'], "%Y-%m-%dT%H:%M:%S%z")
            episode_id = int(episode['id'])
            scheduled_event = await models.ScheduledEvents.get_or_none(episode_id=episode_id)

            try:
                tournament_race = await tournaments.fetch_tournament_handler_v2(event_slug, episode)
            except Exception:
                logging.exception("Error while creating tournament race handler.")
                continue

            name = tournament_race.event_slug.upper()
            if tournament_race.friendly_name:
                name += f" - {tournament_race.friendly_name}"
            if tournament_race.versus:
                name += f" - {tournament_race.versus}"

            name = name[:100]

            description = f"Start Time: {discord.utils.format_dt(start_time, 'f')}"
            end_time = start_time + datetime.timedelta(hours=2)

            if tournament_race.broadcast_channels:
                location = f"https://twitch.tv/{tournament_race.broadcast_channels[0]}"
            elif tournament_race.player_twitch_names:
                location = f"https://multistre.am/{'/'.join(tournament_race.player_twitch_names)}/layout3/"
            else:
                location = "TBD"

            try:
                if scheduled_event:
                    try:
                        event: discord.ScheduledEvent = await event_data.guild.fetch_scheduled_event(
                            scheduled_event.scheduled_event_id)

                        # check if existing event requires an update
                        if not event.name == name or not event.description == description or not event.start_time == start_time or not event.end_time == end_time or not event.location == location:
                            await event.edit(
                                name=name,
                                description=description,
                                start_time=start_time,
                                end_time=end_time,
                                location=location,
                                entity_type=discord.EntityType.external,
                            )
                    except discord.NotFound:
                        event = await event_data.guild.create_scheduled_event(
                            name=name,
                            description=description,
                            start_time=start_time,
                            end_time=end_time,
                            location=location,
                            entity_type=discord.EntityType.external,
                        )
                        await models.ScheduledEvents.update_or_create(episode_id=episode_id,
                                                                      defaults={'scheduled_event_id': event.id,
                                                                                'event_slug': event_slug})
                else:
                    # create an event
                    event = await event_data.guild.create_scheduled_event(
                        name=name,
                        description=description,
                        start_time=start_time,
                        end_time=end_time,
                        location=location
                    )
                    await models.ScheduledEvents.create(scheduled_event_id=event.id, episode_id=episode_id,
                                                        event_slug=event_slug)
            except Exception:
                logging.exception("Unable to create guild event.")

    async def update_scheduling_needs(self, event_data: core.TournamentRace, episodes):
        comms_needed = []
        trackers_needed = []
        broadcasters_needed = []

        for episode in episodes:
            broadcast_channels = [c['slug'] for c in episode['channels'] if
                                  c['id'] not in [0, 31, 36, 62, 63, 64, 65] and c['language'] == event_data.lang]
            if not broadcast_channels:
                continue

            start_time = datetime.datetime.strptime(episode['when'], "%Y-%m-%dT%H:%M:%S%z")
            start_time_string = f"<t:{round(start_time.timestamp())}:f>"

            commentators_approved = [p for p in episode['commentators'] if
                                     p['approved'] and p['language'] == event_data.lang]

            if (c_needed := 2 - len(commentators_approved)) > 0:
                comms_needed += [
                    f"*{start_time_string}* - Need **{c_needed}** - [Sign Up!](http://speedgaming.org/commentator/signup/{episode['id']}/)"]

            trackers_approved = [p for p in episode['trackers'] if p['approved'] and p['language'] == event_data.lang]

            t_needed = (2 if len(episode['match1']['players']) > 2 else 1) - len(trackers_approved)

            if t_needed > 0:
                trackers_needed += [
                    f"*{start_time_string}* - Need **{t_needed}** - [Sign Up!](http://speedgaming.org/tracker/signup/{episode['id']}/)"]

            if broadcast_channels[0] in ['ALTTPRandomizer', 'ALTTPRandomizer2', 'ALTTPRandomizer3', 'ALTTPRandomizer4',
                                         'ALTTPRandomizer5', 'ALTTPRandomizer6']:
                broadcasters_approved = [p for p in episode['broadcasters'] if
                                         p['approved'] and p['language'] == event_data.lang]

                if (b_needed := 1 - len(broadcasters_approved)) > 0:
                    broadcasters_needed += [
                        f"*{start_time_string}* - Need **{b_needed}** - [Sign Up!](http://speedgaming.org/broadcaster/signup/{episode['id']}/)"]

        embed = discord.Embed(
            title="Scheduling Needs",
            description="This is the current scheduling needs for the next 48 hours.\n\nTimes are shown in your **local time zone**.",
            timestamp=datetime.datetime.utcnow()
        )
        embed.add_field(
            name="Commentators Needed",
            value="\n".join(comms_needed) if comms_needed else "No current needs.",
            inline=False
        )
        if event_data.data.scheduling_needs_tracker:
            embed.add_field(
                name="Trackers Needed",
                value="\n".join(trackers_needed) if trackers_needed else "No current needs.",
                inline=False
            )
        if broadcasters_needed:
            embed.add_field(
                name="Broadcasters Needed",
                value="\n".join(broadcasters_needed),
                inline=False
            )

        try:
            bot_message = False
            async for message in event_data.data.scheduling_needs_channel.history(limit=50):
                if message.author == self.bot.user:
                    bot_message = True
                    scheduling_needs_message = message
                    await scheduling_needs_message.edit(embed=embed)
                    break

            if not bot_message:
                await event_data.data.scheduling_needs_channel.send(embed=embed)
        except Exception:
            logging.exception("Unable to update scheduling needs channel.")

    async def create_race_room(self, event_data, event_slug, episode):
        try:
            await tournaments.create_tournament_race_room(event_slug, episode['id'])
        except Exception as e:
            logging.exception(
                "Encountered a problem when attempting to create RT.gg race room.")
            if event_data.audit_channel:
                await event_data.audit_channel.send(
                    f"There was an error while automatically creating a race room for episode `{episode['id']}`.\n\n{str(e)}",
                    allowed_mentions=discord.AllowedMentions(everyone=True)
                )

    async def send_race_form(self, event_data, event_slug, episode):
        try:
            tournament_race = await tournaments.fetch_tournament_handler(event_slug, episode['id'])
            await tournament_race.send_race_submission_form()
        except Exception as e:
            logging.exception("Encountered a problem when attempting send race submission.")
            if event_data.audit_channel:
                await event_data.audit_channel.send(
                    f"There was an error while sending a submission reminder for episode `{episode['id']}`.\n\n{str(e)}",
                    allowed_mentions=discord.AllowedMentions(everyone=True)
                )

    async def report_bad_player_discord(self, event_slug):
        tournament_class = tournaments.TOURNAMENT_DATA[event_slug]

        event_data: core.TournamentConfig = await tournament_class.get_config()
        episodes = await speedgaming.get_upcoming_episodes_by_event(event_slug, hours_past=0, hours_future=48)

        messages = []

        if event_data.guild.chunked is False:
            await event_data.guild.chunk(cache=True)

        for episode in episodes:
            for match in ['match1', 'match2']:
                if episode[match]:
                    for player in episode[match]['players']:
                        if player['publicStream'] == 'ignore':
                            continue

                        if player['displayName'].startswith('Winner of '):
                            continue

                        if player['displayName'].startswith('Loser of '):
                            continue

                        try:
                            member = event_data.guild.get_member(int(player.get('discordId', '')))
                        except ValueError:
                            member = None

                        if member is None:
                            discord_name: str = player.get('discordTag', '')
                            if discord_name.endswith('#0'):  # strip the #0 off the end of the name
                                discord_name = discord_name[:-2]
                            member = event_data.guild.get_member_named(discord_name)

                        if member is None:
                            messages.append(
                                f"Episode {episode['id']} - {event_slug} - {player['displayName']} could not be found")

        return messages

    # @app_commands.command(description="Generate an ALTTPR practice seed from an SG Episode that's already been submitted.")
    # async def practice(self, interaction: discord.Interaction, episode_id: int):
    #     await interaction.response.defer()
    #     tournament_game = await models.TournamentGames.get_or_none(episode_id=episode_id)
    #     if tournament_game is None:
    #         await interaction.response.send_message("That episode has not been submitted yet.", ephemeral=True)
    #         return

    #     settings = tournament_game.settings

    #     seed = await alttpr_discord.ALTTPRDiscord.generate(settings=settings, endpoint='/api/customizer')  # TODO: don't hardcode endpoint
    #     embed = await seed.embed(emojis=self.bot.emojis)

    #     await interaction.followup.send(embed=embed)

    # @app_commands.command(description="Generate a randomizer seed for the ALTTPR Main Tournament 2024.")
    # async def 4(self, interaction: discord.Interaction, player1: discord.Member, player2: discord.Member):
    #     await interaction.response.defer()
    #     seed, preset, deck = await alttpr.roll_seed([player1, player2])

    #     embed = await seed.embed(emojis=self.bot.emojis)
    #     embed.insert_field_at(0, name="Preset", value=preset, inline=False)
    #     if deck:
    #         embed.insert_field_at(1, name="Deck", value="\n".join([f"**{p}**: {c}" for p, c in deck.items()]), inline=False)

    #     await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Generate a randomizer seed for the 2023 challenge cup.")
    @app_commands.guilds(*CC_TOURNAMENT_SERVERS)
    async def cc2023(self, interaction: discord.Interaction, opponent: discord.Member,
                     on_behalf_of: discord.Member = None, player3: discord.Member = None,
                     player4: discord.Member = None):
        if interaction.guild.chunked is False:
            await interaction.guild.chunk(cache=True)

        if on_behalf_of and interaction.guild.get_role(CC_TOURNAMENT_ADMIN_ROLE_ID) not in interaction.user.roles:
            await interaction.response.send_message(
                "You must be a member of the Challenge Cup admin team to roll a seed on someone else's behalf..")
            return

        if (player3 or player4) and interaction.guild.get_role(
                CC_TOURNAMENT_ADMIN_ROLE_ID) not in interaction.user.roles:
            await interaction.response.send_message(
                "You must be a member of the Challenge Cup admin team to roll a seed with more than 2 players.")
            return

        if interaction.user == opponent:
            await interaction.response.send_message("You can't race yourself.", ephemeral=True)
            return

        if on_behalf_of is None:
            on_behalf_of = interaction.user

        if opponent.bot or on_behalf_of.bot:
            await interaction.response.send_message("You can't race a bot.", ephemeral=True)
            return

        players = [opponent, on_behalf_of]
        if player3:
            players.append(player3)
        if player4:
            players.append(player4)

        await interaction.response.defer()
        # embed = await self.generate_deck_seed(players, "cc2023")

        msg_id = None

        if CC_TOURNAMENT_AUDIT_CHANNELS:
            channel = self.bot.get_channel(CC_TOURNAMENT_AUDIT_CHANNELS)
            msg = await channel.send("Generating...")
            msg_id = msg.id

        seed, preset, deck = await alttpr.roll_seed(players, event_slug="cc2023", episode_id=msg_id)

        embed = await seed.embed(emojis=self.bot.emojis, include_settings=False)
        embed.insert_field_at(0, name="Preset", value=preset, inline=False)
        if deck:
            embed.insert_field_at(1, name="Deck", value="\n".join([f"**{p}**: {c}" for p, c in deck.items()]),
                                  inline=False)

        embed.title = " vs. ".join([p.display_name for p in players])
        embed.description = " vs. ".join([p.mention for p in players])

        for player in players:
            await player.send(embed=embed)

        if CC_TOURNAMENT_AUDIT_CHANNELS:
            await msg.edit(content="Sent to players.", embed=embed, view=ChallengeCupDeleteHistoryView())

        await interaction.followup.send("Seed successfully sent to DM.")

    @app_commands.command(
        description="Generate a hypothetical deck for a match.  This does not generate a seed or write to history.")
    @app_commands.guilds(*CC_TOURNAMENT_SERVERS, *MAIN_TOURNAMENT_SERVERS)
    @app_commands.choices(
        event_slug=[
            app_commands.Choice(name="cc2024", value="cc2024"),
            app_commands.Choice(name="alttpr2024", value="alttpr2024"),
        ]
    )
    async def tournament_deck(self, interaction: discord.Interaction, opponent: discord.Member,
                              on_behalf_of: discord.Member = None, event_slug: str = None):
        if on_behalf_of is None:
            on_behalf_of = interaction.user

        if interaction.user == opponent:
            await interaction.response.send_message("You must specify two different players.", ephemeral=True)
            return

        if opponent.bot or on_behalf_of.bot:
            await interaction.response.send_message("You cannot specify a bot as a player.", ephemeral=True)
            return

        if event_slug is None:
            if interaction.guild.id in CC_TOURNAMENT_SERVERS:
                event_slug = "cc2023"
            elif interaction.guild.id in MAIN_TOURNAMENT_SERVERS:
                event_slug = "alttpr2024"
            else:
                await interaction.response.send_message("You must specify an event slug.", ephemeral=True)
                return

        deck = await alttpr.generate_deck([opponent, on_behalf_of], event_slug=event_slug)
        preset = random.choices(list(deck.keys()), weights=list(deck.values()))[0]
        embed = discord.Embed(
            title=f"{opponent.display_name} vs. {on_behalf_of.display_name}",
            description=f"{opponent.mention} vs. {on_behalf_of.mention}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Deck", value="\n".join([f"**{p}**: {c}" for p, c in deck.items()]), inline=False)
        embed.add_field(name="In this hypothetical matchup, this mode was drawn:", value=preset, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tournament(bot))

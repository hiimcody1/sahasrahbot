import datetime

from quart import (Blueprint, abort, jsonify, redirect,
                   render_template, request, url_for, Response)
from quart_discord import requires_authorization, Unauthorized
from tortoise.contrib.pydantic import pydantic_model_creator, pydantic_queryset_creator

from alttprbot import models
from alttprbot.util import asynctournament
from alttprbot_api import auth
from alttprbot_api.api import discord
from alttprbot_api.util import checks

asynctournament_blueprint = Blueprint('async', __name__)


@asynctournament_blueprint.route('/api/tournaments', methods=['GET'])
@auth.authorized_key('asynctournament')
async def tournaments_api():
    filter_args = {}
    if request.args.get('active'):
        filter_args['active'] = request.args.get('active') == 'true'

    qs = models.AsyncTournament.filter(**filter_args)
    AsyncTournament_Pydantic_List = pydantic_queryset_creator(models.AsyncTournament)
    res = await AsyncTournament_Pydantic_List.from_queryset(qs)
    return Response(res.json(), mimetype='application/json')


@asynctournament_blueprint.route('/api/tournaments/<int:tournament_id>', methods=['GET'])
@auth.authorized_key('asynctournament')
async def tournament_api(tournament_id):
    result = await models.AsyncTournament.get_or_none(id=tournament_id)
    if result is None:
        return jsonify({'error': 'Tournament not found.'})

    AsyncTournament_Pydantic = pydantic_model_creator(models.AsyncTournament)
    res = await AsyncTournament_Pydantic.from_tortoise_orm(result)
    return Response(res.json(), mimetype='application/json')


@asynctournament_blueprint.route('/api/tournaments/<int:tournament_id>/races', methods=['GET'])
@auth.authorized_key('asynctournament')
async def races_api(tournament_id):
    filter_args = {}
    if request.args.get('id'):
        filter_args['id'] = request.args.get('id')
    if request.args.get('user_id'):
        filter_args['user_id'] = request.args.get('user_id')
    if request.args.get('permalink_id'):
        filter_args['permalink_id'] = request.args.get('permalink_id')
    if request.args.get('pool_id'):
        filter_args['permalink__pool_id'] = request.args.get('pool_id')
    if request.args.get('status'):
        filter_args['status'] = request.args.get('status')

    if request.args.get('page'):
        page = int(request.args.get('page'))
    else:
        page = 1

    if request.args.get('page_size'):
        page_size = int(request.args.get('page_size'))
        if page_size > 100:
            return abort(400, 'page_size cannot be greater than 100.')
    else:
        page_size = 20

    qs = models.AsyncTournamentRace.filter(tournament_id=tournament_id, **filter_args).offset(
        (page - 1) * page_size).limit(page_size)
    AsyncTournamentRace_Pydantic_List = pydantic_queryset_creator(models.AsyncTournamentRace)

    res = await AsyncTournamentRace_Pydantic_List.from_queryset(qs)
    return Response(res.json(), mimetype='application/json')


@asynctournament_blueprint.route('/api/tournaments/<int:tournament_id>/pools', methods=['GET'])
@auth.authorized_key('asynctournament')
async def pools_api(tournament_id):
    filter_args = {}
    if request.args.get('id'):
        filter_args['id'] = request.args.get('id')

    qs = models.AsyncTournamentPermalinkPool.filter(tournament_id=tournament_id, **filter_args)
    AsyncTournamentPermalinkPool_Pydantic_List = pydantic_queryset_creator(models.AsyncTournamentPermalinkPool)
    res = await AsyncTournamentPermalinkPool_Pydantic_List.from_queryset(qs)
    return Response(res.json(), mimetype='application/json')


@asynctournament_blueprint.route('/api/tournaments/<int:tournament_id>/permalinks', methods=['GET'])
@auth.authorized_key('asynctournament')
async def permalinks_api(tournament_id):
    filter_args = {}
    if request.args.get('id'):
        filter_args['id'] = request.args.get('id')
    if request.args.get('pool_id'):
        filter_args['pool_id'] = request.args.get('pool_id')

    qs = models.AsyncTournamentPermalink.filter(pool__tournament_id=tournament_id, **filter_args)
    AsyncTournamentPermalink_Pydantic_List = pydantic_queryset_creator(models.AsyncTournamentPermalink)
    res = await AsyncTournamentPermalink_Pydantic_List.from_queryset(qs)
    return Response(res.json(), mimetype='application/json')


@asynctournament_blueprint.route('/api/tournaments/<int:tournament_id>/leaderboard', methods=['GET'])
@auth.authorized_key('asynctournament')
async def leaderboard_api(tournament_id):
    tournament = await models.AsyncTournament.get_or_none(id=tournament_id)
    if tournament is None:
        return jsonify({'error': 'Tournament not found.'})

    leaderboard = await asynctournament.get_leaderboard(tournament)

    return jsonify([
        {
            'player': {
                'id': e.player.id,
                'display_name': e.player.display_name,
                'discord_user_id': e.player.discord_user_id,
                'twitch_name': e.player.twitch_name,
                'rtgg_id': e.player.rtgg_id,
            },
            'score': e.score,
            'rank': idx + 1,
            'races': [
                {
                    'id': race.id,
                    'start_time': race.start_time,
                    'end_time': race.end_time,
                    'score': race.score,
                    'permalink_id': race.permalink_id,
                    'elapsed_time': race.elapsed_time_formatted,
                    'status': race.status,
                } if race else None
                for race in e.races
            ],
            'counts': {
                'finished': e.finished_race_count,
                'forfeited': e.forfeited_race_count,
                'unplayed': e.unattempted_race_count,
            }
        }
        for idx, e in enumerate(leaderboard)
    ])

# public dashboard for the current tournament player
@asynctournament_blueprint.route('/races/<int:tournament_id>', methods=['GET'])
@requires_authorization
async def async_tournament(tournament_id: int):
    discord_user = await discord.fetch_user()
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)
    tournament = await models.AsyncTournament.get(id=tournament_id)
    races = await models.AsyncTournamentRace.filter(user=user, tournament=tournament).order_by('-created').prefetch_related('permalink', 'permalink__pool')

    reattempted = bool([r for r in races if r.reattempted is True])
        

    return await render_template('asynctournament_dashboard.html', user=discord_user, player=user, tournament=tournament, races=races, reattempted=reattempted)

@asynctournament_blueprint.route('/races/<int:tournament_id>/reattempt', methods=['GET'])
@requires_authorization
async def async_tournament_reattempt(tournament_id: int):
    discord_user = await discord.fetch_user()
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)
    tournament = await models.AsyncTournament.get(id=tournament_id)
    race_id = request.args.get('race_id', None)

    reattempted_races = await models.AsyncTournamentRace.filter(user=user, tournament=tournament, reattempted=True)
    if reattempted_races:
        return abort(403, "You have already reattempted a race in this tournament.  Please contact a tournament admin if you believe this is in error.")

    if race_id is None:
        return abort(400, "You must supply a race_id")

    # verify user of race matches logged in user
    race = await models.AsyncTournamentRace.get_or_none(id=race_id, user=user, tournament=tournament)
    if not race:
        return abort(403, "You must be the player of this race to reattempt it.")


    return await render_template('asynctournament_reattempt.html', user=discord_user, player=user, tournament=tournament, race=race)

@asynctournament_blueprint.route('/race/<int:tournament_id>/reattempt', methods=['POST'])
@requires_authorization
async def async_tournament_reattempt_submit(tournament_id: int):
    discord_user = await discord.fetch_user()
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)
    tournament = await models.AsyncTournament.get(id=tournament_id)

    reattempted_races = await models.AsyncTournamentRace.filter(user=user, tournament=tournament, reattempted=True)
    if reattempted_races:
        return abort(403, "You have already reattempted a race in this tournament.  Please contact a tournament admin if you believe this is in error.")

    race_id = request.args.get('race_id', None)
    if race_id is None:
        return abort(400, "You must supply a race_id")

    race = await models.AsyncTournamentRace.get_or_none(id=race_id, user=user, tournament=tournament)
    if not race:
        return abort(403, "You must be the player of this race to reattempt it.")

    payload = await request.form
    reason = payload.get('reason', None)

    race.reattempted = True
    race.reattempt_reason = reason

    await race.save()

    return redirect(url_for('async.async_tournament', tournament_id=tournament_id))

@asynctournament_blueprint.route('/races/<int:tournament_id>/queue', methods=['GET'])
@requires_authorization
async def async_tournament_queue(tournament_id: int):
    discord_user = await discord.fetch_user()
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)

    page = int(request.args.get('page', 1))
    page_size = 20

    request_filter = {}

    if not (status := request.args.get('status', 'finished')) == 'all':
        request_filter['status'] = status

    if not (reviewer := request.args.get('reviewed', 'all')) == 'all':
        if reviewer == 'unreviewed':
            request_filter['reviewed_by'] = None
        elif reviewer == 'me':
            request_filter['reviewed_by'] = user
        else:
            try:
                request_filter['reviewed_by_id'] = int(reviewer)
            except ValueError:
                pass

    if not (review_status := request.args.get('review_status', 'pending')) == 'all':
        request_filter['review_status'] = review_status

    if not (live := request.args.get('live', 'false')) == 'all':
        request_filter['thread_id__isnull'] = live == 'true'

    tournament = await models.AsyncTournament.get(id=tournament_id)

    authorized = await checks.is_async_tournament_user(user, tournament, ['admin', 'mod'])
    if not authorized:
        return abort(403, "You are not authorized to view this tournament.")

    races = await tournament.races.filter(reattempted=False, **request_filter).offset((page - 1) * page_size).limit(
        page_size).prefetch_related('user', 'reviewed_by', 'permalink', 'permalink__pool', 'tournament')

    return await render_template(
        'asynctournament_race_list.html',
        user=discord_user,
        tournament=tournament,
        races=races,
        status=status,
        reviewer=reviewer,
        review_status=review_status,
        live=live,
        page=page
    )


@asynctournament_blueprint.route('/races/<int:tournament_id>/review/<int:race_id>', methods=['GET'])
@requires_authorization
async def async_tournament_review(tournament_id: int, race_id: int):
    discord_user = await discord.fetch_user()
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)

    tournament = await models.AsyncTournament.get(id=tournament_id)

    reviewable = True

    authorized = await checks.is_async_tournament_user(user, tournament, ['admin', 'mod'])
    if not authorized:
        reviewable = False  # doesn't matter for now
        return abort(403, "You are not authorized to view this tournament.")

    race = await models.AsyncTournamentRace.get_or_none(id=race_id, tournament=tournament)
    if race is None:
        abort(404, "Race not found.")

    if race.status != 'finished':
        reviewable = False

    if race.reattempted:
        reviewable = False

    await race.fetch_related('user', 'reviewed_by', 'permalink', 'permalink__pool', 'tournament', 'live_race')

    # if race.user == user:
    #     abort(403, "You are not authorized to review your own tournament run.")

    if race.reviewed_by is None and reviewable:
        race.reviewed_by = user
        await race.save()

    return await render_template('asynctournament_race_view.html', user=discord_user,
                                 tournament=tournament, race=race, already_claimed=race.reviewed_by != user,
                                 reviewable=reviewable)


@asynctournament_blueprint.route('/races/<int:tournament_id>/review/<int:race_id>', methods=['POST'])
@requires_authorization
async def async_tournament_review_submit(tournament_id: int, race_id: int):
    discord_user = await discord.fetch_user()
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)

    tournament = await models.AsyncTournament.get(id=tournament_id)

    authorized = await checks.is_async_tournament_user(user, tournament, ['admin', 'mod'])
    if not authorized:
        return abort(403, "You are not authorized to view this tournament.")

    race = await models.AsyncTournamentRace.get_or_none(id=race_id, tournament=tournament)
    if race is None:
        abort(404, "Race not found.")

    if race.status != 'finished':
        abort(403, "This race cannot be reviewed yet.")

    if race.reattempted:
        abort(403, "This race was marked as reattempted and cannot be reviewed.")

    if race.user == user:
        abort(403, "You are not authorized to review your own tournament run.")

    payload = await request.form

    race.review_status = payload.get('review_status', 'pending')
    race.reviewer_notes = payload.get('reviewer_notes', None)
    race.reviewed_at = datetime.datetime.now()
    race.reviewed_by = user

    await race.save()

    return redirect(url_for("async.async_tournament_queue", tournament_id=tournament_id))


@asynctournament_blueprint.route('/races/<int:tournament_id>/leaderboard', methods=['GET'])
# @requires_authorization
async def async_tournament_leaderboard(tournament_id: int):
    try:
        discord_user = await discord.fetch_user()
    except Unauthorized:
        discord_user = None
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)

    tournament = await models.AsyncTournament.get(id=tournament_id)

    authorized = await checks.is_async_tournament_user(user, tournament, ['admin', 'mod'])
    if not authorized:
        return abort(403, "You are not authorized to view this tournament.")

    leaderboard = await asynctournament.get_leaderboard(tournament)

    if estimate := request.args.get('estimate', 'false') == 'true':
        sort_key = "estimate"
    else:
        sort_key = "score"

    return await render_template('asynctournament_leaderboard.html',
                                 user=discord_user, tournament=tournament, leaderboard=leaderboard, estimate=estimate,
                                 sort_key=sort_key)


@asynctournament_blueprint.route('/player/<int:tournament_id>/<int:user_id>', methods=['GET'])
@requires_authorization
async def async_tournament_player(tournament_id: int, user_id: int):
    try:
        discord_user = await discord.fetch_user()
    except Unauthorized:
        discord_user = None
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)

    tournament = await models.AsyncTournament.get(id=tournament_id)

    authorized = await checks.is_async_tournament_user(user, tournament, ['admin', 'mod', 'public'])
    if not authorized:
        return abort(403, "You are not authorized to view this tournament.")

    player = await models.Users.get_or_none(id=user_id)
    races = await models.AsyncTournamentRace.filter(tournament=tournament, user_id=user_id).order_by(
        '-created').prefetch_related('tournament', 'user', 'permalink', 'permalink__pool')

    return await render_template('asynctournament_user.html', user=discord_user,
                                 races=races, tournament=tournament, player=player)


@asynctournament_blueprint.route('/pools/<int:tournament_id>', methods=['GET'])
@requires_authorization
async def async_tournament_pools(tournament_id: int):
    try:
        discord_user = await discord.fetch_user()
    except Unauthorized:
        discord_user = None
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)

    tournament = await models.AsyncTournament.get(id=tournament_id)

    authorized = await checks.is_async_tournament_user(user, tournament, ['admin', 'mod', 'public'])
    if not authorized:
        return abort(403, "You are not authorized to view this tournament.")

    await tournament.fetch_related('permalink_pools', 'permalink_pools__permalinks')

    return await render_template('asynctournament_pools.html', user=discord_user,
                                 tournament=tournament)


@asynctournament_blueprint.route('/permalink/<int:tournament_id>/<int:permalink_id>', methods=['GET'])
@requires_authorization
async def async_tournament_permalink(tournament_id: int, permalink_id: int):
    try:
        discord_user = await discord.fetch_user()
    except Unauthorized:
        discord_user = None
    user = await models.Users.get_or_none(discord_user_id=discord_user.id)

    tournament = await models.AsyncTournament.get(id=tournament_id)
    permalink = await models.AsyncTournamentPermalink.get(id=permalink_id, pool__tournament=tournament)

    # skip authorization check if this was a live race
    if permalink.live_race is False:
        authorized = await checks.is_async_tournament_user(user, tournament, ['admin', 'mod', 'public'])
        if not authorized:
            return abort(403, "You are not authorized to view this tournament.")

    races = await permalink.races.filter(status__in=['finished', 'forfeit'], reattempted=False).order_by(
        '-score').prefetch_related('live_race')

    return await render_template('asynctournament_permalink_view.html', user=discord_user, tournament=tournament, permalink=permalink, races=races)

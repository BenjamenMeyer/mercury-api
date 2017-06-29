import bson
import logging

from bottle import route, run, request, HTTPResponse
from mercury.common.inventory_client.client import InventoryClient
from mercury.common.exceptions import MercuryCritical, MercuryUserError

from mercury_api.configuration import api_configuration


log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


inventory_configuration = api_configuration.get('inventory', {})
inventory_router_url = inventory_configuration.get('inventory_router')

if not inventory_router_url:
    raise MercuryCritical('Configuration is missing or invalid')

inventory_client = InventoryClient(inventory_router_url)


def http_error(message, code=500):
    return HTTPResponse({'error': True, 'message': message}, status=code)


def validate_json(f):
    def wrapper(*args, **kwargs):
        try:
            if not request.json:
                return http_error('JSON request is missing', code=400)
        except ValueError:
            log.debug('JSON request is malformed: {}'.format(request.body.read()))
            return http_error('JSON request is malformed', code=400)

        return f(*args, **kwargs)

    return wrapper


def check_query(f):
    def wrapper(*args, **kwargs):
        if not isinstance(request.json.get('query'), dict):
            return http_error('JSON request is malformed', code=400)
        return f(*args, **kwargs)
    return wrapper


def get_projection_from_qsa():
    projection_keys = request.query.get('projection', '')
    projection = {}
    if projection_keys:
        for k in projection_keys.split(','):
            projection[k] = 1

    return projection or None


def get_paging_info_from_qsa():
    _d = {
        'limit': 250,  # TODO: Move this to configuration files
        'offset_id': None,
        'sort_direction': 1
    }
    limit = request.query.get('limit')
    offset_id = request.query.get('offset_id')
    sort_direction = request.query.get('sort_direction')

    if limit and limit.isdigit():
        _d['limit'] = int(limit)

    if bson.ObjectId.is_valid(offset_id):
        _d['offset_id'] = offset_id

    try:
        _d['sort_direction'] = int(sort_direction)
    except (TypeError, ValueError):  # None == TypeError, anything else == ValueError
        pass

    return _d


def convert_id(doc):
    if '_id' in doc:
        doc['_id'] = str(doc['_id'])
    return doc


def doc_transformer(doc):
    if not doc:
        return

    convert_id(doc)
    ttl_time_completed = doc.get('ttl_time_completed')
    if ttl_time_completed:
        doc['ttl_time_completed'] = ttl_time_completed.ctime()

    return doc


@route('/api/inventory/computers', method='GET')
def computers():
    projection = get_projection_from_qsa()
    paging_data = get_paging_info_from_qsa()
    return inventory_client.query({}, projection=projection,
                                  limit=paging_data['limit'],
                                  sort_direction=paging_data['sort_direction'])


@route('/api/inventory/computers/query', method='POST')
@validate_json
@check_query
def computers_query():
    query = request.json.get('query')
    projection = get_projection_from_qsa()
    paging_data = get_paging_info_from_qsa()
    log.debug('QUERY: {}'.format(query))

    return inventory_client.query(query, projection=projection,
                                  limit=paging_data['limit'],
                                  sort_direction=paging_data['sort_direction'])


@route('/api/inventory/computers/count', method='POST')
@validate_json
@check_query
def computer_query_count():
    query = request.json.get('query')
    return {'count': inventory_client.count(query)}


@route('/api/inventory/computers/<mercury_id>', method='GET')
def computer(mercury_id):
    projection = get_projection_from_qsa()
    c = inventory_client.get_one(mercury_id, projection=projection)

    if not c:
        return http_error('mercury_id %s does not exist in inventory' % mercury_id,
                          404)

    return c


@route('/api/active/computers', method='GET')
def active_computers():
    projection = get_projection_from_qsa()
    paging_data = get_paging_info_from_qsa()
    if not projection:
        print('projection: ' + str(projection))
        projection = {'active': 1, 'mercury_id': 1}

    return inventory_client.query({'active': {'ne': None}},
                                  projection=projection,
                                  limit=paging_data['limit'],
                                  sort_direction=paging_data['sort_direction'])


@route('/api/active/computers/<mercury_id>', method='GET')
def active_computer(mercury_id):
    projection = get_projection_from_qsa()
    if not projection:
        projection = {'active': 1, 'mercury_id': 1}

    c = inventory_client.get_one({'mercury_id': mercury_id}, projection=projection)

    if not c:
        return http_error('mercury_id %s does not exist in inventory' % mercury_id,
                          404)
    return c


@route('/api/active/computers/query', method='POST')
@validate_json
@check_query
def active_computer_query():
    query = request.json.get('query')

    # Make sure we get only active devices
    query.update({'active': {'$ne': None}})
    projection = get_projection_from_qsa()
    paging_data = get_paging_info_from_qsa()
    return inventory_client.query(query, projection=projection,
                                  limit=paging_data['limit'],
                                  sort_direction=paging_data['sort_direction'])


@route('/api/rpc/jobs/<job_id>', method='GET')
def get_job(job_id):
    projection = get_projection_from_qsa()
    job = jobs_collection.find_one({'job_id': job_id}, projection=projection)
    if not job:
        return http_error('Job not found', code=404)
    return {'job': doc_transformer(job)}


@route('/api/rpc/jobs/<job_id>/status', method='GET')
def get_job_status(job_id):
    error_states = ['ERROR', 'TIMEOUT', 'EXCEPTION']
    job = jobs_collection.find_one({'job_id': job_id})
    if not job:
        return http_error('Job not found', code=404)
    tasks = tasks_collection.find({'job_id': job_id}, {'task_id': 1, 'status': 1, '_id': 0})

    job['has_failures'] = False
    job['tasks'] = []

    for task in tasks:
        job['tasks'].append(convert_id(task))
        if task['status'] in error_states:
            job['has_failures'] = True

    log.debug(convert_id(job))
    return {'job': doc_transformer(job)}


@route('/api/rpc/tasks/<job_id>', method='GET')
def get_tasks(job_id):
    projection = get_projection_from_qsa()
    c = tasks_collection.find({'job_id': job_id}, projection=projection)
    count = c.count()
    tasks = []
    for task in c:
        tasks.append(doc_transformer(task))
    return {'count': count, 'tasks': tasks}


@route('/api/rpc/task/<task_id>')
def get_task(task_id):
    task = tasks_collection.find_one({'task_id': task_id})
    if not task:
        return http_error('Task not found', code=404)
    return {'task': doc_transformer(task)}


@route('/api/rpc/jobs', method='GET')
def get_jobs():
    projection = get_projection_from_qsa() or {'instruction': 0}
    c = jobs_collection.find({}, projection=projection).sort('time_created', 1)
    count = c.count()
    jobs = []
    for job in c:
        jobs.append(doc_transformer(job))
    return {'count': count, 'jobs': jobs}


def get_all_active_query(query):
    query.update({'active': {'$ne': None}})
    return inventory_client.query(query, projection={'active': 1}, limit=0, sort_direction=1)['items']


@route('/api/rpc/jobs', method='POST')
@validate_json
@check_query
def post_jobs():
    instruction = request.json.get('instruction')

    if not isinstance(instruction, dict):
        return http_error('Command is missing from request or is malformed', code=400)

    query = request.json.get('query')

    active_matches = get_all_active_query(query)

    active_match_count = len(active_matches)
    log.debug('Matched %d active computers' % active_match_count)

    if not active_match_count:
        return http_error('query did not match any active records', code=400)

    try:
        job = Job(instruction, active_matches, jobs_collection, tasks_collection)
    except MercuryUserError as mue:
        return http_error(str(mue), code=400)
    job.start()

    return {'job_id': str(job.job_id)}


run(host='0.0.0.0', port=9005, debug=True)

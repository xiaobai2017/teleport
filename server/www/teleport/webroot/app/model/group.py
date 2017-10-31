# -*- coding: utf-8 -*-

from app.const import *
from app.base.logger import log
from app.base.db import get_db, SQL
from app.base.utils import tp_timestamp_utc_now
from app.model import syslog


def create(handler, gtype, name, desc):
    if gtype not in TP_GROUP_TYPES:
        return TPE_PARAM, 0

    db = get_db()
    _time_now = tp_timestamp_utc_now()

    # 1. 判断是否已经存在了
    sql = 'SELECT id FROM {dbtp}group WHERE type={gtype} AND name="{gname}";'.format(dbtp=db.table_prefix, gtype=gtype, gname=name)
    db_ret = db.query(sql)
    if db_ret is not None and len(db_ret) > 0:
        return TPE_EXISTS, 0

    operator = handler.get_current_user()

    # 2. 插入记录
    sql = 'INSERT INTO `{dbtp}group` (`type`, `name`, `creator_id`, `create_time`, `desc`) VALUES ' \
          '({gtype}, "{gname}", {creator_id}, {create_time}, "{desc}");' \
          ''.format(dbtp=db.table_prefix,
                    gtype=gtype, gname=name, creator_id=operator['id'],
                    create_time=_time_now, desc=desc)
    db_ret = db.exec(sql)
    if not db_ret:
        return TPE_DATABASE, 0

    _id = db.last_insert_id()

    syslog.sys_log(operator, handler.request.remote_ip, TPE_OK, "创建{gtype}：{gname}".format(gtype=TP_GROUP_TYPES[gtype], gname=name))

    return TPE_OK, _id


def lock(handler, gtype, glist):
    if gtype not in TP_GROUP_TYPES:
        return TPE_PARAM

    group_list = [str(i) for i in glist]

    db = get_db()

    # 2. 更新记录
    sql = 'UPDATE `{dbtp}group` SET state={state} WHERE id IN ({gids});' \
          ''.format(dbtp=db.table_prefix, state=TP_STATE_DISABLED, gids=','.join(group_list))
    db_ret = db.exec(sql)
    if not db_ret:
        return TPE_DATABASE

    return TPE_OK


def unlock(handler, gtype, glist):
    if gtype not in TP_GROUP_TYPES:
        return TPE_PARAM

    group_list = [str(i) for i in glist]

    db = get_db()

    # 2. 更新记录
    sql = 'UPDATE `{dbtp}group` SET state={state} WHERE id IN ({gids});' \
          ''.format(dbtp=db.table_prefix, state=TP_STATE_NORMAL, gids=','.join(group_list))
    db_ret = db.exec(sql)
    if not db_ret:
        return TPE_DATABASE

    return TPE_OK


def remove(handler, gtype, glist):
    if gtype not in TP_GROUP_TYPES:
        return TPE_PARAM

    group_list = [str(i) for i in glist]

    # 1. 获取组的名称，用于记录系统日志
    where = 'g.type={gtype} AND g.id IN ({gids})'.format(gtype=gtype, gids=','.join(group_list))

    s = SQL(get_db())
    err = s.select_from('group', ['name'], alt_name='g').where(where).query()
    if err != TPE_OK:
        return err
    if len(s.recorder) == 0:
        return TPE_NOT_EXISTS

    name_list = [n['name'] for n in s.recorder]

    # 删除组与成员的映射关系
    where = 'type={} AND gid IN ({})'.format(gtype, ','.join(group_list))
    err = s.reset().delete_from('group_map').where(where).exec()
    if err != TPE_OK:
        return err

    # 删除组
    where = 'type={gtype} AND id IN ({gids})'.format(gtype=gtype, gids=','.join(group_list))
    err = s.reset().delete_from('group').where(where).exec()
    if err != TPE_OK:
        return err

    # 记录系统日志
    syslog.sys_log(handler.get_current_user(), handler.request.remote_ip, TPE_OK, "删除{gtype}：{gname}".format(gtype=TP_GROUP_TYPES[gtype], gname='，'.join(name_list)))

    return TPE_OK


def get_by_id(gtype, gid):
    # 获取要查询的组的信息
    s = SQL(get_db())
    s.select_from('group', ['id', 'state', 'name', 'desc'], alt_name='g')
    s.where('g.type={} AND g.id={}'.format(gtype, gid))
    err = s.query()
    if err != TPE_OK:
        return err, {}
    if len(s.recorder) == 0:
        return TPE_NOT_EXISTS, {}
    return TPE_OK, s.recorder[0]


def get_list(gtype):
    s = SQL(get_db())
    s.select_from('group', ['id', 'name'], alt_name='g')
    s.where('g.type={}'.format(gtype))

    err = s.query()
    return err, s.recorder


def update(handler, gid, name, desc):
    db = get_db()

    # 1. 判断是否已经存在
    sql = 'SELECT id FROM {}group WHERE id={};'.format(db.table_prefix, gid)
    db_ret = db.query(sql)
    if db_ret is None or len(db_ret) == 0:
        return TPE_NOT_EXISTS

    # 2. 更新记录
    sql = 'UPDATE `{}group` SET name="{name}", desc="{desc}" WHERE id={gid};' \
          ''.format(db.table_prefix, name=name, desc=desc, gid=gid)
    db_ret = db.exec(sql)
    if not db_ret:
        return TPE_DATABASE

    return TPE_OK


def add_members(gtype, gid, members):
    db = get_db()
    sql = []
    for uid in members:
        sql.append('INSERT INTO `{}group_map` (type, gid, mid) VALUES ({}, {}, {});'.format(db.table_prefix, gtype, gid, uid))
    if db.transaction(sql):
        return TPE_OK
    else:
        return TPE_DATABASE


def remove_members(gtype, gid, members):
    db = get_db()

    _where = 'WHERE (type={gtype} AND gid={gid} AND mid IN ({mid}))'.format(gtype=gtype, gid=gid, mid=','.join([str(uid) for uid in members]))

    sql = 'DELETE FROM `{dbtp}group_map` {where};'.format(dbtp=db.table_prefix, where=_where)
    if db.exec(sql):
        return TPE_OK
    else:
        return TPE_DATABASE


def make_groups(handler, gtype, glist, failed):
    """
    根据传入的组列表，查询每个组的名称对应的id，如果没有，则创建之
    """
    db = get_db()
    _time_now = tp_timestamp_utc_now()

    operator = handler.get_current_user()
    name_list = list()

    for g in glist:
        sql = 'SELECT id FROM {dbtp}group WHERE type={gtype} AND name="{gname}";'.format(dbtp=db.table_prefix, gtype=gtype, gname=g)
        db_ret = db.query(sql)
        if db_ret is None or len(db_ret) == 0:
            # need create group.
            sql = 'INSERT INTO `{dbtp}group` (`type`, `name`, `creator_id`, `create_time`) VALUES ' \
                  '({gtype}, "{name}", {creator_id}, {create_time});' \
                  ''.format(dbtp=db.table_prefix,
                            gtype=gtype, name=g, creator_id=operator['id'], create_time=_time_now)

            db_ret = db.exec(sql)
            if not db_ret:
                failed.append({'line': 0, 'error': '创建{gtype} `{gname}` 失败，写入数据库时发生错误'.format(gtype=TP_GROUP_TYPES[gtype], gname=g)})
                continue

            glist[g] = db.last_insert_id()
            name_list.append(g)

        else:
            glist[g] = db_ret[0][0]

    syslog.sys_log(operator, handler.request.remote_ip, TPE_OK, "创建{gtype}：{gname}".format(gtype=TP_GROUP_TYPES[gtype], gname='，'.join(name_list)))
    return TPE_OK


def make_group_map(gtype, gm):
    db = get_db()
    for item in gm:
        # 检查如果不存在，则插入
        sql = 'SELECT id FROM `{dbtp}group_map` WHERE type={gtype} AND gid={gid} AND mid={mid};'.format(dbtp=db.table_prefix, gtype=gtype, gid=item['gid'], mid=item['mid'])
        db_ret = db.query(sql)
        if db_ret is None or len(db_ret) == 0:
            sql = 'INSERT INTO `{dbtp}group_map` (`type`, `gid`, `mid`) VALUES ' \
                  '({gtype}, {gid}, {mid});' \
                  ''.format(dbtp=db.table_prefix, gtype=gtype, gid=item['gid'], mid=item['mid'])
            db_ret = db.exec(sql)

# def make_account_groups(handler, group_list, failed):
#     """
#     根据传入的组列表，查询每个组的名称对应的id，如果没有，则创建之
#     """
#     db = get_db()
#     _time_now = tp_timestamp_utc_now()
#
#     for g in group_list:
#         sql = 'SELECT id FROM {}group WHERE type=3 AND name="{}";'.format(db.table_prefix, g)
#         db_ret = db.query(sql)
#         if db_ret is None or len(db_ret) == 0:
#             # need create group.
#             sql = 'INSERT INTO `{}group` (`type`, `name`, `creator_id`, `create_time`) VALUES ' \
#                   '(3, "{name}", {creator_id}, {create_time});' \
#                   ''.format(db.table_prefix,
#                             name=g, creator_id=handler.get_current_user()['id'], create_time=_time_now)
#
#             db_ret = db.exec(sql)
#             if not db_ret:
#                 failed.append({'line': 0, 'error': '创建账号组 `{}` 失败，写入数据库时发生错误'.format(g)})
#                 continue
#
#             # success.append(user['account'])
#             group_list[g] = db.last_insert_id()
#
#         else:
#             group_list[g] = db_ret[0][0]
#
#     return TPE_OK


def get_groups(sql_filter, sql_order, sql_limit, sql_restrict, sql_exclude):
    print(sql_filter)

    dbtp = get_db().table_prefix
    s = SQL(get_db())
    s.select_from('group', ['id', 'state', 'name', 'desc'], alt_name='g')

    str_where = ''
    _where = list()

    # if len(sql_restrict) > 0:
    #     for k in sql_restrict:
    #         if k == 'ops_policy_id':
    #             _where.append('g.id NOT IN (SELECT rid FROM {dbtp}ops_auz WHERE policy_id={pid} AND rtype=2)'.format(dbtp=dbtp, pid=sql_exclude[k]))
    #         else:
    #             log.w('unknown restrict field: {}\n'.format(k))

    if len(sql_exclude) > 0:
        for k in sql_exclude:
            # if k == 'group_id':
            #     _where.append('u.id NOT IN (SELECT mid FROM {dbtp}group_map WHERE type={gtype} AND gid={gid})'.format(dbtp=dbtp, gtype=TP_GROUP_USER, gid=sql_exclude[k]))
            if k == 'ops_policy_id':
                pid = sql_exclude[k]['pid']
                gtype = sql_exclude[k]['gtype']
                _where.append('g.id NOT IN (SELECT rid FROM {dbtp}ops_auz WHERE policy_id={pid} AND rtype={rtype})'.format(dbtp=dbtp, pid=pid, rtype=gtype))
            else:
                log.w('unknown exclude field: {}\n'.format(k))

    if len(sql_filter) > 0:
        for k in sql_filter:
            if k == 'type':
                _where.append('g.type={filter}'.format(filter=sql_filter[k]))
            elif k == 'state':
                _where.append('g.state={filter}'.format(filter=sql_filter[k]))
            elif k == 'search':
                _where.append('(g.name LIKE "%{filter}%" OR g.desc LIKE "%{filter}%")'.format(filter=sql_filter[k]))
            else:
                log.e('unknown filter field: {}\n'.format(k))
                return TPE_PARAM, 0, 0, {}

    if len(_where) > 0:
        str_where = '( {} )'.format(' AND '.join(_where))

    s.where(str_where)

    if sql_order is not None:
        _sort = False if not sql_order['asc'] else True
        if 'name' == sql_order['name']:
            s.order_by('g.name', _sort)
        elif 'state' == sql_order['name']:
            s.order_by('g.state', _sort)
        else:
            log.e('unknown order field: {}\n'.format(sql_order['name']))
            return TPE_PARAM, 0, 0, {}

    if len(sql_limit) > 0:
        s.limit(sql_limit['page_index'], sql_limit['per_page'])

    err = s.query()
    return err, s.total_count, s.page_index, s.recorder
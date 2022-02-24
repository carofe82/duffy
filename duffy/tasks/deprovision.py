from collections import defaultdict
from typing import List

from celery.utils.log import get_task_logger
from sqlalchemy import select

from ..database import sync_session_maker
from ..database.model import Node
from ..database.types import NodeState
from .base import celery
from .mechanisms import MechanismFailure
from .node_pools import ConcreteNodePool, NodePool

log = get_task_logger(__name__)

NODE_FIELDS_TO_CONSIDER = {"id", "hostname", "ipaddr", "data"}


@celery.task(bind=True)
def deprovision_pool_nodes(self, pool_name: str, node_ids: List[int]):
    log.debug("[%s] Deprovisioning nodes from pool (begin): %r", pool_name, node_ids)

    try:
        pool = NodePool.known_pools[pool_name]
    except KeyError:
        log.error("[%s] Can't find pool.", pool_name)
        return

    if not isinstance(pool, ConcreteNodePool):
        log.error("[%s] Pool must be a concrete node pool.", pool_name)
        return

    try:
        with sync_session_maker() as db_sync_session, db_sync_session.begin():
            nodes = (
                db_sync_session.execute(
                    select(Node)
                    .filter_by(active=True, pool=pool_name, state=NodeState.deployed)
                    .filter(Node.id.in_(node_ids))
                )
                .scalars()
                .all()
            )

            for node in nodes:
                node.state = NodeState.deprovisioning
                node.pool = None

        found_node_ids = {node.id for node in nodes}
        not_found_node_ids = set(node_ids) - found_node_ids

        if not_found_node_ids:
            log.warning(
                "[%s] Didn't find deployed nodes with ids: %s", pool_name, not_found_node_ids
            )

        log.debug("[%s] Attempting to deprovision nodes: %r", pool_name, found_node_ids)

        with sync_session_maker() as db_sync_session, db_sync_session.begin():
            nodes = [db_sync_session.merge(node, load=False) for node in nodes]

            try:
                deprov_result = pool.deprovision(nodes)
            except MechanismFailure:
                log.error("[%s] Deprovisioning mechanism failed.", pool.name)
                log.debug("[%s] Marking nodes as failed in database.", pool.name)
                with sync_session_maker() as db_sync_session_in_exc, db_sync_session_in_exc.begin():
                    for node in nodes:
                        exc_node = db_sync_session_in_exc.merge(node, load=False)
                        exc_node.data["error"] = "deprovisioning failed"
                        exc_node.state = NodeState.failed
                raise

            unmatched_nodes = set(nodes)
            matched_nodes = set()

            for node_res in deprov_result["nodes"]:
                # match up nodes with the data blurb from their provisioning
                for node in unmatched_nodes:
                    matched = False

                    for fname in NODE_FIELDS_TO_CONSIDER:
                        if fname not in node_res:
                            continue

                        if getattr(node, fname) != node_res[fname]:
                            matched = False
                            break

                        matched = True

                    if matched:
                        # at least one field considered was matched
                        matched_nodes.add(node)
                        unmatched_nodes.remove(node)
                        break
                else:
                    # didn't break out of loop -> no node object matched
                    log.warning("[%s] Node result couldn't be matched: %r", pool.name, node_res)

            if unmatched_nodes:
                # handle & report nodes the apparently weren't deprovisioned
                unmatched_ids = []
                for node in unmatched_nodes:
                    unmatched_ids.append(node.id)
                    node.state = NodeState.failed

                log.warning("[%s] Nodes unmatched in result: %r", pool.name, sorted(unmatched_ids))

            # clean up DB objects of deprovisioned nodes
            for node in matched_nodes:
                if node.reusable:
                    node.state = NodeState.unused
                else:
                    node.state = NodeState.done
                    node.active = False
    except Exception:
        log.error("[%s] Deprovisioning failed: %r", pool_name, node_ids)
        raise

    log.debug("[%s] Deprovisioning nodes from pool (end): %r", pool_name, node_ids)


@celery.task
def deprovision_nodes(node_ids: List[int]):
    """Deprovision nodes e.g. of an expired session.

    This divides up nodes by their pools and kicks off sub tasks for
    each pool.
    """
    log.debug("deprovision_nodes(%r) begin", node_ids)
    pools_node_ids = defaultdict(list)

    with sync_session_maker() as db_sync_session, db_sync_session.begin():
        # First, find the -- active, deployed -- nodes with the supplied ids, sort them by their
        # pools and change their state to 'deprovisioning', then kick off sub tasks which
        # deprovision all nodes that belong to the same pool.
        nodes = (
            db_sync_session.execute(
                select(Node)
                .filter_by(active=True, state=NodeState.deployed)
                .filter(Node.id.in_(node_ids))
            )
            .scalars()
            .all()
        )

        found_node_ids = {node.id for node in nodes}
        not_found_node_ids = sorted(set(node_ids) - found_node_ids)

        if not_found_node_ids:
            log.warning("Didn't find deployed nodes with ids: %s", not_found_node_ids)

        for node in nodes:
            pools_node_ids[node.pool].append(node.id)

    for pool_name, node_ids in pools_node_ids.items():
        log.debug("Creating task to deprovision session nodes in pool %s", pool_name)
        deprovision_pool_nodes.delay(pool_name=pool_name, node_ids=node_ids).forget()

    log.debug("deprovision_nodes(%r) end", node_ids)
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(eq=False)
class ResourcePool:
    """
    A group of interchangeable production resources: same company, same
    capacities and eligible for exactly the same resource types. The
    optimizer schedules against the pool (one variable set instead of one
    per member); assignments are split back to members afterwards.
    """

    pk: str
    members: list = field(default_factory=list)
    company_id: int = 0
    parallel_capacity: int = 0
    daily_capacity_minutes: int = 0
    counts_running_time_once: bool = False

    @property
    def company(self):
        return self.members[0].company

    def __str__(self):
        return " / ".join(str(m) for m in self.members)


def pool_interchangeable_resources(
    resource_type_active_resources,
    resource_parallel_capacity,
    resource_daily_capacity_minutes,
    resource_slot_load,
    resource_day_load,
):
    """
    Replaces concrete resources with ResourcePools in the optimizer's data
    dictionaries. Resources are grouped only when they are truly
    interchangeable: same company, same parallel/daily capacity and members
    of exactly the same set of resource types (so cross-stage capacity
    sharing stays correct). Pool capacity and pre-existing load are the sum
    over members.

    Returns the pooled versions of the four resource dictionaries.
    """
    memberships = defaultdict(set)
    for rt, resources in resource_type_active_resources.items():
        for resource in resources:
            memberships[resource].add(rt)

    groups = defaultdict(list)
    for resource, rts in memberships.items():
        counts_running_time_once = getattr(resource, "counts_running_time_once", False)
        # Unattended machines keep individual running-time budgets.
        key = (
            frozenset(rts),
            resource.company_id,
            resource_parallel_capacity[resource],
            resource_daily_capacity_minutes[resource],
            counts_running_time_once,
            resource.pk if counts_running_time_once else None,
        )
        groups[key].append(resource)

    pooled_rt_resources = {rt: [] for rt in resource_type_active_resources}
    pooled_parallel_capacity = {}
    pooled_daily_capacity = {}
    member_to_pool = {}

    ordered_groups = sorted(
        groups.items(), key=lambda kv: min(m.pk for m in kv[1])
    )
    for index, (key, members) in enumerate(ordered_groups):
        rts, _company_id, parallel, daily, running_once, _member_id = key
        members = sorted(members, key=lambda m: m.pk)
        pool = ResourcePool(
            pk=f"pool{index}",
            members=members,
            company_id=members[0].company_id,
            parallel_capacity=parallel * len(members),
            daily_capacity_minutes=daily * len(members),
            counts_running_time_once=running_once,
        )
        for rt in rts:
            pooled_rt_resources[rt].append(pool)
        pooled_parallel_capacity[pool] = pool.parallel_capacity
        pooled_daily_capacity[pool] = pool.daily_capacity_minutes
        for member in members:
            member_to_pool[member] = pool

    pooled_slot_load = defaultdict(int)
    for (resource, pos), load in resource_slot_load.items():
        pooled_slot_load[(member_to_pool[resource], pos)] += load

    pooled_day_load = defaultdict(int)
    for (resource, day), load in resource_day_load.items():
        pooled_day_load[(member_to_pool[resource], day)] += load

    return (
        pooled_rt_resources,
        pooled_parallel_capacity,
        pooled_daily_capacity,
        pooled_slot_load,
        pooled_day_load,
    )

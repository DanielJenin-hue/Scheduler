from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.scheduling.contract_payroll import (
    aggregate_payroll_contract_hours,
    period_contract_hours_for_fte,
)
from lab_scheduler.scheduling.portage_template import (
    PortageMasterLineSpec,
    portage_pattern_for_bucket,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


@dataclass(frozen=True, slots=True)
class PoolMember:
    """One interchangeable seat in a role capacity bucket."""

    employee_id: str
    role: str
    bucket_index: int
    contract_line_type: str
    fte: float


@dataclass(frozen=True, slots=True)
class ElasticPoolManager:
    """
    Runtime pool of interchangeable MLT/MLA capacity buckets.

    Staff are grouped by qualification role (MLT or MLA). Bucket indices are
    assigned from a stable sort on employee id so new hires receive the next
    available index without hard-coded line names.
    """

    members: Mapping[str, PoolMember]
    role_pools: Mapping[str, Tuple[str, ...]]
    employees_by_id: Mapping[str, EmployeeProfile]

    @classmethod
    def from_employees(
        cls,
        employees: Sequence[EmployeeProfile],
        *,
        qual_codes: Optional[Mapping[str, str]] = None,
    ) -> ElasticPoolManager:
        grouped: Dict[str, list[EmployeeProfile]] = {"MLT": [], "MLA": []}
        employees_by_id: Dict[str, EmployeeProfile] = {}

        for employee in employees:
            employees_by_id[employee.id] = employee
            role = infer_qual_code(employee, qual_codes=qual_codes)
            if role not in grouped:
                grouped[role] = []
            grouped[role].append(employee)

        members: Dict[str, PoolMember] = {}
        role_pools: Dict[str, Tuple[str, ...]] = {}

        for role, role_employees in grouped.items():
            ordered = sorted(role_employees, key=lambda emp: emp.id)
            member_ids: list[str] = []
            for bucket_index, employee in enumerate(ordered):
                contract = (employee.contract_line_type or "D/E").upper()
                members[employee.id] = PoolMember(
                    employee_id=employee.id,
                    role=role,
                    bucket_index=bucket_index,
                    contract_line_type=contract,
                    fte=float(employee.fte or 0.0),
                )
                member_ids.append(employee.id)
            role_pools[role] = tuple(member_ids)

        return cls(
            members=members,
            role_pools=role_pools,
            employees_by_id=employees_by_id,
        )

    def staff_count(self, role: Optional[str] = None) -> int:
        if role is None:
            return len(self.members)
        return len(self.role_pools.get(role, ()))

    def member_for(self, employee_id: str) -> Optional[PoolMember]:
        return self.members.get(employee_id)

    def role_for(self, employee_id: str) -> Optional[str]:
        member = self.members.get(employee_id)
        return member.role if member is not None else None

    def master_line_spec_for(self, employee_id: str) -> Optional[PortageMasterLineSpec]:
        employee = self.employees_by_id.get(employee_id)
        if employee is not None:
            from lab_scheduler.scheduling.portage_template import (
                parse_vacant_portage_line,
                portage_master_line_spec,
            )

            if parse_vacant_portage_line(employee.full_name) is not None:
                return portage_master_line_spec(employee)

        member = self.members.get(employee_id)
        if member is None or employee is None:
            return None
        return portage_pattern_for_bucket(
            role=member.role,
            contract_line_type=member.contract_line_type,
            fte=member.fte or float(employee.fte or 0.0),
            bucket_index=member.bucket_index,
        )

    def role_capacity_hours(
        self,
        role: str,
        *,
        rules: JurisdictionRules,
        weeks_in_period: int,
    ) -> float:
        member_ids = self.role_pools.get(role, ())
        total = 0.0
        for employee_id in member_ids:
            member = self.members[employee_id]
            total += period_contract_hours_for_fte(
                fte=member.fte,
                weeks_in_period=weeks_in_period,
                standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
            )
        return round(total, 2)

    def total_capacity_hours(
        self,
        *,
        rules: JurisdictionRules,
        weeks_in_period: int,
    ) -> float:
        return aggregate_payroll_contract_hours(
            [self.employees_by_id[employee_id] for employee_id in self.members],
            rules=rules,
            weeks_in_period=weeks_in_period,
        )

    def pool_average_hours(
        self,
        employee_id: str,
        *,
        rules: JurisdictionRules,
        weeks_in_period: int,
    ) -> float:
        member = self.members.get(employee_id)
        if member is None:
            return 0.0
        member_ids = self.role_pools.get(member.role, ())
        if not member_ids:
            return 0.0
        role_capacity = self.role_capacity_hours(
            member.role,
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        return round(role_capacity / len(member_ids), 2)

    def load_reference_hours_map(
        self,
        *,
        rules: JurisdictionRules,
        weeks_in_period: int,
    ) -> Dict[str, float]:
        """Even-distribution reference load per employee (role pool average)."""

        references: Dict[str, float] = {}
        for role, member_ids in self.role_pools.items():
            if not member_ids:
                continue
            average = self.role_capacity_hours(
                role,
                rules=rules,
                weeks_in_period=weeks_in_period,
            ) / len(member_ids)
            for employee_id in member_ids:
                references[employee_id] = round(average, 2)
        return references

    def bucket_continuity_penalty(
        self,
        employee_id: str,
        *,
        employee_total_hours: Mapping[str, float],
        load_reference_hours: Mapping[str, float],
    ) -> float:
        """
        Prefer filling lower-index buckets before higher-index peers in the same role
        when those peers remain below the pool average load.
        """

        member = self.members.get(employee_id)
        if member is None:
            return 0.0

        my_hours = float(employee_total_hours.get(employee_id, 0.0))
        my_reference = float(load_reference_hours.get(employee_id, 0.0))

        for other_id in self.role_pools.get(member.role, ()):
            other = self.members[other_id]
            if other.bucket_index >= member.bucket_index:
                continue
            other_hours = float(employee_total_hours.get(other_id, 0.0))
            other_reference = float(load_reference_hours.get(other_id, my_reference))
            if other_hours + 1e-6 < other_reference:
                return float(member.bucket_index - other.bucket_index) * 10.0
        if my_hours + 8.0 < my_reference:
            return float(member.bucket_index) * 2.0
        return 0.0

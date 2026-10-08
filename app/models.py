from typing import Optional

from pydantic import BaseModel


class AccountCreate(BaseModel):
    name: str
    institution: str = "other"
    account_type: str = "credit"
    last4: Optional[str] = None


class CategoryCreate(BaseModel):
    name: str
    color: str = "#9E9E9E"
    is_transfer: bool = False
    group_id: Optional[int] = None
    budget_frequency: str = "monthly"


class CategoryUpdate(BaseModel):
    name: str
    color: str
    is_transfer: bool = False
    group_id: Optional[int] = None
    budget_frequency: str = "monthly"


class CategoryGroupCreate(BaseModel):
    name: str
    color: str = "#9E9E9E"


class CategoryGroupUpdate(BaseModel):
    name: str
    color: str


class RuleCreate(BaseModel):
    pattern: str
    match_type: str = "contains"
    category_id: int
    priority: int = 150
    apply_to_existing: bool = True


class TransactionUpdate(BaseModel):
    category_id: int
    apply_to_similar: bool = False


class SplitItem(BaseModel):
    category_id: int
    amount: float


class SplitsUpdate(BaseModel):
    splits: list[SplitItem]


class TrimRequest(BaseModel):
    before: str  # ISO date YYYY-MM-DD; deletes transactions dated earlier than this


class ReassignRequest(BaseModel):
    account_id: int


class MergeCategoryRequest(BaseModel):
    target_category_id: int


class StatementTotalUpdate(BaseModel):
    statement_total: Optional[float] = None


class StatementSave(BaseModel):
    account_id: int
    period_end: str  # ISO date the statement closes
    new_balance: float
    period_start: Optional[str] = None  # blank = day after the previous statement's closing date
    previous_balance: Optional[float] = None  # blank = the previous statement's new balance


class ReconcileRequest(BaseModel):
    account_id: Optional[int] = None


class BudgetEntry(BaseModel):
    category_id: int
    amount: float


class BudgetSaveRequest(BaseModel):
    year: int
    month: int
    entries: list[BudgetEntry]

from typing import List, Optional

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    keywords: List[str] = Field(default_factory=list)
    location: str = "Singapore"
    offsets: List[int] = Field(default_factory=lambda: [0])
    limit: int = 50
    easy_apply: bool = True
    employment_types: List[str] = Field(default_factory=list)
    experience_levels: List[str] = Field(default_factory=list)
    distance_miles: Optional[int] = None
    under_10_applicants: bool = False
    score_threshold: int = 65
    formats: List[str] = Field(default_factory=lambda: ["docx", "md"])
    dry_run: bool = False


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    master_resume: Optional[str] = None
    assessment_prompt: Optional[str] = None
    default_params: Optional[dict] = None
    default_formats: Optional[List[str]] = None

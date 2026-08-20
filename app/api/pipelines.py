"""Pipeline discovery.

Dharini calls this to learn what pipelines exist and to render their parameter
forms from the returned JSON Schema. That is what makes adding a pipeline a
backend-only change -- the frontend asks rather than being told.
"""

from fastapi import APIRouter, HTTPException

from ..pipelines import PipelineNotFound, registry
from ..schemas import PipelineOut

router = APIRouter(prefix="/v1/pipelines", tags=["pipelines"])


@router.get("", response_model=list[PipelineOut])
def list_pipelines():
    return registry.describe_all()


@router.get("/{pipeline_id}", response_model=PipelineOut)
def get_pipeline(pipeline_id: str):
    try:
        return registry.get(pipeline_id).describe()
    except PipelineNotFound:
        raise HTTPException(status_code=404, detail=f"unknown pipeline: {pipeline_id}") from None

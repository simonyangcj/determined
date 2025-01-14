package command

import (
	"context"
	"database/sql"

	"github.com/pkg/errors"
	"github.com/uptrace/bun"

	"github.com/determined-ai/determined/master/internal/db"
	"github.com/determined-ai/determined/master/pkg/model"
)

// GetCommandOwnerID gets a command's ownerID from a taskID. Uses persisted command state.
// Returns db.ErrNotFound if a command with given taskID does not exist.
func GetCommandOwnerID(ctx context.Context, taskID model.TaskID) (model.UserID, error) {
	ownerIDBun := &struct {
		bun.BaseModel `bun:"table:command_state"`
		OwnerID       model.UserID `bun:"owner_id"`
	}{}

	if err := db.Bun().NewSelect().Model(ownerIDBun).
		ColumnExpr("generic_command_spec->'Base'->'Owner'->'id' AS owner_id").
		Where("task_id = ?", taskID).
		Scan(ctx); err != nil {
		if errors.Cause(err) == sql.ErrNoRows {
			return 0, db.ErrNotFound
		}
		return 0, err
	}

	return ownerIDBun.OwnerID, nil
}

// TaskMetadata captures minimal metadata about a task.
type TaskMetadata struct {
	bun.BaseModel `bun:"table:command_state"`
	WorkspaceID   model.AccessScopeID `bun:"workspace_id"`
	TaskType      model.TaskType      `bun:"task_type"`
	ExperimentIDs []int32             `bun:"experiment_ids"`
	TrialIDs      []int32             `bun:"trial_ids"`
}

// IdentifyTask returns the task metadata for a given task ID.
// Returns db.ErrNotFound if a command with given taskID does not exist.
func IdentifyTask(ctx context.Context, taskID model.TaskID) (TaskMetadata, error) {
	metadata := TaskMetadata{}
	if err := db.Bun().NewSelect().Model(&metadata).
		ColumnExpr("generic_command_spec->'Metadata'->'workspace_id' AS workspace_id").
		// TODO(DET-10004) TaskType needs
		// to have ->> instead of -> so task_type doesn't get surrounded by double quotes.
		ColumnExpr("generic_command_spec->'TaskType' as task_type").
		ColumnExpr("generic_command_spec->'Metadata'->'experiment_ids' as experiment_ids").
		ColumnExpr("generic_command_spec->'Metadata'->'trial_ids' as trial_ids").
		Where("task_id = ?", taskID).
		Scan(ctx); err != nil {
		if errors.Cause(err) == sql.ErrNoRows {
			return metadata, db.ErrNotFound
		}
		return metadata, err
	}
	return metadata, nil
}

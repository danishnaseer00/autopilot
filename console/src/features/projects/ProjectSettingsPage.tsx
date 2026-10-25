import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import type { BranchRule, Environment, ProjectPatch } from "../../api/types";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { TextInput } from "../../components/Field";
import { LoadingPanel } from "../../components/Loading";
import { ErrorState } from "../../components/ProblemView";
import { NotFoundPage } from "../../components/StatusPages";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { useProjectDeletionImpact } from "../../hooks/useProjectDeletionImpact";
import { formatDateTime, pluralize } from "../../lib/format";
import { fieldErrors } from "../../lib/problem";
import { useProject } from "../../hooks/useProject";

export default function ProjectSettingsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useProject(projectId);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [defaultBranch, setDefaultBranch] = useState("");
  const [branches, setBranches] = useState<BranchRule[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState<unknown>(null);
  const initialised = useRef<string | null>(null);

  useDocumentTitle(project.data ? `${project.data.name} · Settings` : "Project settings");

  useEffect(() => {
    if (project.data && initialised.current !== project.data.id) {
      initialised.current = project.data.id;
      setName(project.data.name);
      setDefaultBranch(project.data.default_branch);
      setBranches(project.data.branches);
    }
  }, [project.data]);

  const impact = useProjectDeletionImpact(projectId ?? "", deleteOpen);

  const save = useMutation({
    mutationFn: (patch: ProjectPatch) => api.projects.update(projectId ?? "", patch),
    onSuccess: (updated) => {
      queryClient.setQueryData(["project", projectId], updated);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      setSaved(true);
      setErrors({});
      setTimeout(() => setSaved(false), 4000);
    },
    onError: (error) => {
      const apiErrors = fieldErrors(error);
      if (Object.keys(apiErrors).length > 0) setErrors(apiErrors);
    },
  });

  const remove = useMutation({
    mutationFn: () => api.projects.remove(projectId ?? ""),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      queryClient.removeQueries({ queryKey: ["project", projectId] });
      navigate("/projects", { replace: true });
    },
    onError: (error) => setDeleteError(error),
  });

  if (!projectId) return <NotFoundPage />;
  if (project.isPending) return <LoadingPanel label="Loading settings" />;
  if (project.isError) {
    return <ErrorState error={project.error} onRetry={() => void project.refetch()} />;
  }

  const data = project.data;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next: Record<string, string> = {};
    if (name.trim().length < 2 || name.trim().length > 64) {
      next.name = "Use a name of 2 to 64 characters.";
    }
    if (!defaultBranch.trim()) next.default_branch = "Choose a default branch.";
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    save.mutate({
      name: name.trim(),
      default_branch: defaultBranch.trim(),
      branches,
    });
  }

  function updateBranch(index: number, patch: Partial<BranchRule>) {
    setBranches((current) =>
      current.map((branch, branchIndex) =>
        branchIndex === index ? { ...branch, ...patch } : branch,
      ),
    );
  }

  const impactText = impact.isPending
    ? "Counting deployments…"
    : impact.data
      ? `${impact.data.complete ? "" : "at least "}${pluralize(impact.data.count, "deployment")}, ${pluralize(data.domains.length, "domain")} and ${pluralize(data.secrets.length, "secret")} will be deleted.`
      : "The deployment count could not be read.";

  return (
    <div className="stack stack--loose">
      <form className="panel form" onSubmit={submit} noValidate>
        <div className="panel__header">
          <div>
            <h2>Project settings</h2>
            <p className="panel__subtitle">
              Changes use <code>PATCH /projects/&#123;id&#125;</code> and invalidate the project
              cache by key.
            </p>
          </div>
        </div>

        {saved ? (
          <p className="success-message" role="status" aria-live="polite">
            Settings saved.
          </p>
        ) : null}
        {save.isError && Object.keys(errors).length === 0 ? (
          <ErrorState
            error={save.error}
            compact
            onRetry={() => void project.refetch()}
          />
        ) : null}

        <TextInput
          id="settings-name"
          label="Project name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          error={errors.name}
          hint="Tenant-supplied text is rendered as text, never as HTML."
          required
        />
        <TextInput
          id="settings-branch"
          label="Default branch"
          value={defaultBranch}
          onChange={(event) => setDefaultBranch(event.target.value)}
          error={errors.default_branch}
          hint="Production deployments triggered without a git ref use this branch."
          required
        />

        <fieldset className="fieldset">
          <legend>Branch to environment mapping</legend>
          {branches.length === 0 ? (
            <p className="text-muted">No branch rules are configured.</p>
          ) : (
            <div className="table-wrap">
              <table className="data-table data-table--compact">
                <thead>
                  <tr>
                    <th scope="col">Git ref</th>
                    <th scope="col">Environment</th>
                    <th scope="col">Auto-deploy</th>
                  </tr>
                </thead>
                <tbody>
                  {branches.map((branch, index) => (
                    <tr key={`${branch.git_ref}-${index}`}>
                      <td>
                        <code>{branch.git_ref}</code>
                      </td>
                      <td>
                        <label className="visually-hidden" htmlFor={`branch-env-${index}`}>
                          Environment for {branch.git_ref}
                        </label>
                        <select
                          id={`branch-env-${index}`}
                          className="select select--compact"
                          value={branch.environment}
                          onChange={(event) =>
                            updateBranch(index, { environment: event.target.value as Environment })
                          }
                        >
                          <option value="production">production</option>
                          <option value="preview">preview</option>
                        </select>
                      </td>
                      <td>
                        <label className="checkbox-inline">
                          <input
                            type="checkbox"
                            checked={branch.auto_deploy}
                            onChange={(event) =>
                              updateBranch(index, { auto_deploy: event.target.checked })
                            }
                            aria-label={`Auto-deploy ${branch.git_ref} to ${branch.environment}`}
                          />
                          <span>Auto-deploy</span>
                        </label>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </fieldset>

        <div className="form__actions">
          <Button type="submit" variant="primary" busy={save.isPending}>
            Save settings
          </Button>
        </div>
      </form>

      <section className="panel panel--danger" aria-labelledby="danger-title">
        <h2 id="danger-title">Danger zone</h2>
        <p>
          Deleting a project soft-deletes the record and removes it from this console. Its domains,
          secrets and deployments stop being addressable.
        </p>
        <p className="text-muted">
          Project created {formatDateTime(data.created_at)}. This action cannot be undone from the
          console.
        </p>
        <Button variant="danger" onClick={() => setDeleteOpen(true)}>
          Delete project
        </Button>
        {deleteError ? (
          <ErrorState
            error={deleteError}
            compact
            onReload={() => {
              setDeleteError(null);
              void project.refetch();
            }}
          />
        ) : null}
      </section>

      <ConfirmDialog
        open={deleteOpen}
        title={`Delete ${data.name}?`}
        description={
          <p>
            This permanently removes the project from the console. The API performs a soft delete
            (SPEC-36 §3.2), but the console has no restore flow.
          </p>
        }
        consequence={`Deletes ${data.name}: ${impactText} This cannot be undone.`}
        confirmLabel="Delete project"
        requireText={data.slug}
        requireTextLabel={`Type the project slug (${data.slug}) to confirm`}
        busy={remove.isPending}
        onCancel={() => setDeleteOpen(false)}
        onConfirm={() => {
          setDeleteError(null);
          remove.mutate();
        }}
      />
    </div>
  );
}

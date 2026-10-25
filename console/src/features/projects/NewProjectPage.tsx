import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import { isApiError } from "../../api/errors";
import type { CreateProjectRequest } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { Button, buttonClass } from "../../components/Button";
import { Callout } from "../../components/Callout";
import { EmptyState } from "../../components/EmptyState";
import { TextInput } from "../../components/Field";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState } from "../../components/ProblemView";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { fieldErrors } from "../../lib/problem";

export default function NewProjectPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [rootPath, setRootPath] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  useDocumentTitle("New project");

  const org = auth.activeOrg?.slug ?? "";

  const create = useMutation({
    mutationFn: (body: CreateProjectRequest) => api.projects.create(body),
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects", org] });
      navigate(`/projects/${project.id}`, { replace: true });
    },
    onError: (error) => {
      const apiErrors = fieldErrors(error);
      if (Object.keys(apiErrors).length > 0) setErrors(apiErrors);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next: Record<string, string> = {};
    const trimmed = name.trim();
    if (trimmed.length < 2 || trimmed.length > 64) {
      next.name = "Use a name of 2 to 64 characters.";
    }
    if (rootPath && !rootPath.startsWith("/")) {
      next.root_path = "Use a path beginning with /, for example /apps/web.";
    }
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    create.mutate({ org, name: trimmed, root_path: rootPath.trim() || undefined });
  }

  if (!auth.activeOrg) {
    return (
      <>
        <PageHeader title="New project" />
        <EmptyState
          eyebrow="No active organisation"
          title="Choose an organisation first"
          action={<Link to="/projects">Return to projects</Link>}
        >
          Your session has no active organisation, so there is nowhere to create a project. Select
          an organisation, then return to this page.
        </EmptyState>
      </>
    );
  }

  return (
    <>
      <PageHeader
        eyebrow={auth.activeOrg.name}
        title="New project"
        description="Create the project record first. Local fixture projects can deploy immediately; connecting a real GitHub repository is a separate OAuth step."
      />

      {create.isError && Object.keys(errors).length === 0 ? (
        <ErrorState
          error={create.error}
          compact
          onRetry={() => {
            if (!isApiError(create.error) || create.error.code !== "validation_failed") create.reset();
          }}
        />
      ) : null}

      <form className="panel form" onSubmit={submit} noValidate>
        <TextInput
          id="project-name"
          label="Project name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          hint="Shown in the console. 2–64 characters. Tenant text is always rendered as text."
          error={errors.name}
          required
          autoComplete="off"
        />
        <TextInput
          id="project-root"
          label="Repository root path"
          value={rootPath}
          onChange={(event) => setRootPath(event.target.value)}
          hint="Optional. Use / when the app is at the repository root, or a subdirectory such as /apps/web."
          error={errors.root_path}
          placeholder="/"
          autoComplete="off"
        />

        <Callout tone="info" title="What happens next">
          The project is created with a local fixture repository, so you can trigger a deployment
          right away. GitHub OAuth and webhook delivery are not implemented yet; a connected
          repository needs the commit SHA supplied by its webhook.
        </Callout>

        <div className="form__actions">
          <Button type="submit" variant="primary" busy={create.isPending}>
            Create project
          </Button>
          <Link className={buttonClass("secondary")} to="/projects">
            Cancel
          </Link>
        </div>
      </form>
    </>
  );
}

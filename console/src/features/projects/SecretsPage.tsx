import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import type { SecretTarget, PutSecretResponse } from "../../api/types";
import { Button } from "../../components/Button";
import { Callout } from "../../components/Callout";
import { Checkbox, TextInput } from "../../components/Field";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { CopyButton } from "../../components/CopyButton";
import { EmptyState } from "../../components/EmptyState";
import { LoadingPanel } from "../../components/Loading";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { formatRelative } from "../../lib/format";
import { fieldErrors } from "../../lib/problem";

const TARGETS: Array<{ value: SecretTarget; title: string; description: string }> = [
  {
    value: "runtime",
    title: "Runtime only",
    description: "Injected into the running service. Not available to build steps.",
  },
  {
    value: "build",
    title: "Build-visible",
    description:
      "Visible to repository code during a build. Anyone who can change that code can read the value.",
  },
  {
    value: "both",
    title: "Build and runtime",
    description: "Visible to build code and injected at runtime. Highest exposure.",
  },
];

export default function SecretsPage() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const queryClient = useQueryClient();
  useDocumentTitle("Secrets");

  const secrets = useQuery({
    queryKey: ["secrets", projectId],
    queryFn: ({ signal }) => api.secrets.list(projectId, signal),
    enabled: Boolean(projectId),
  });

  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [target, setTarget] = useState<SecretTarget>("runtime");
  const [environments, setEnvironments] = useState<string[]>(["production"]);
  const [confirmation, setConfirmation] = useState(false);
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});
  const [created, setCreated] = useState<PutSecretResponse | null>(null);
  const [deleteKey, setDeleteKey] = useState<string | null>(null);
  const [writeError, setWriteError] = useState<unknown>(null);
  const [deleteError, setDeleteError] = useState<unknown>(null);

  const needsConfirmation =
    target === "build" || target === "both" || environments.some((env) => env !== "production");

  const put = useMutation({
    mutationFn: () =>
      api.secrets.put(projectId, key.trim(), {
        value,
        target,
        environments,
        confirmation,
      }),
    onSuccess: (data) => {
      setCreated(data);
      setKey("");
      setValue("");
      setTarget("runtime");
      setEnvironments(["production"]);
      setConfirmation(false);
      setFormErrors({});
      setWriteError(null);
      void queryClient.invalidateQueries({ queryKey: ["secrets", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: (error) => {
      setWriteError(error);
      const apiErrors = fieldErrors(error);
      if (Object.keys(apiErrors).length > 0) setFormErrors(apiErrors);
    },
  });

  const remove = useMutation({
    mutationFn: (secretKey: string) => api.secrets.remove(projectId, secretKey),
    onSuccess: () => {
      setDeleteKey(null);
      setDeleteError(null);
      void queryClient.invalidateQueries({ queryKey: ["secrets", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: (error) => setDeleteError(error),
  });

  function toggleEnvironment(environment: string, checked: boolean) {
    setEnvironments((current) =>
      checked
        ? [...new Set([...current, environment])]
        : current.filter((entry) => entry !== environment),
    );
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next: Record<string, string> = {};
    if (!/^[A-Z_][A-Z0-9_]{0,127}$/.test(key.trim())) {
      next.key = "Use upper-case letters, numbers and underscores, starting with a letter or underscore.";
    }
    if (!value) next.value = "A value is required.";
    if (environments.length === 0) next.environments = "Choose at least one environment.";
    if (needsConfirmation && !confirmation) {
      next.confirmation =
        "Confirm that this value may be visible to build code or a non-production environment.";
    }
    setFormErrors(next);
    setWriteError(null);
    if (Object.keys(next).length > 0) return;
    put.mutate();
  }

  const selectedDelete = secrets.data?.find((secret) => secret.key === deleteKey) ?? null;

  return (
    <div className="stack stack--loose">
      {created ? (
        <section className="panel panel--secret" aria-labelledby="secret-once-title" aria-live="polite">
          <p className="eyebrow">One-time value</p>
          <h2 id="secret-once-title">Copy {created.key} now</h2>
          <p>
            This is the only time the API returns this value (SPEC-36 §3.4 / AC-3). The console does
            not put it in the query cache, local storage or the page URL.
          </p>
          <div className="secret-once">
            <code>{created.value}</code>
            <CopyButton value={created.value} label="Copy value" />
          </div>
          <Button variant="secondary" onClick={() => setCreated(null)}>
            I have stored it; hide the value
          </Button>
        </section>
      ) : null}

      <section className="panel" aria-labelledby="secrets-list-title">
        <div className="panel__header">
          <div>
            <h2 id="secrets-list-title">Secret metadata</h2>
            <p className="panel__subtitle">
              Keys, visibility, environments and last use. Values are never fetched for display.
            </p>
          </div>
        </div>

        {secrets.isPending ? <LoadingPanel label="Loading secrets" lines={3} /> : null}
        {secrets.isError ? (
          <ErrorState error={secrets.error} onRetry={() => void secrets.refetch()} />
        ) : null}
        {secrets.data?.length === 0 ? (
          <EmptyState title="No secrets stored">
            <p>
              Add the first key with the form below. Runtime-only secrets should be the default;
              build visibility is a deliberate, alarming choice.
            </p>
          </EmptyState>
        ) : null}
        {secrets.data && secrets.data.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <caption className="visually-hidden">Secret metadata for this project</caption>
              <thead>
                <tr>
                  <th scope="col">Key</th>
                  <th scope="col">Visibility</th>
                  <th scope="col">Environments</th>
                  <th scope="col">Last used</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {secrets.data.map((secret) => (
                  <tr key={secret.key}>
                    <td>
                      <code className="table-primary">{secret.key}</code>
                    </td>
                    <td>
                      <StatusBadge status={secret.target} srPrefix="Visibility" />
                      {secret.target === "build" || secret.target === "both" ? (
                        <span className="table-secondary">Readable by build code</span>
                      ) : null}
                    </td>
                    <td>{secret.environments.join(", ")}</td>
                    <td>{secret.last_used_at ? formatRelative(secret.last_used_at) : "Never"}</td>
                    <td>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => {
                          setDeleteError(null);
                          setDeleteKey(secret.key);
                        }}
                      >
                        Delete
                        <span className="visually-hidden"> {secret.key}</span>
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <form className="panel form" onSubmit={submit} noValidate aria-labelledby="secret-form-title">
        <div className="panel__header">
          <div>
            <h2 id="secret-form-title">Add or rotate a secret</h2>
            <p className="panel__subtitle">
              Writing an existing key rotates it. The value is sent once over TLS and returned once
              for confirmation.
            </p>
          </div>
        </div>

        {writeError && Object.keys(formErrors).length === 0 ? (
          <ErrorState error={writeError} compact />
        ) : null}

        <TextInput
          id="secret-key"
          label="Key"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          hint="Upper-case letters, numbers and underscores. Stored exactly as typed."
          error={formErrors.key}
          autoComplete="off"
          spellCheck={false}
          required
        />
        <TextInput
          id="secret-value"
          label="Value"
          type="password"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          hint="Never displayed again after this write. Use a secret manager as the source of truth."
          error={formErrors.value}
          autoComplete="new-password"
          required
        />

        <fieldset className="fieldset">
          <legend>Where can this value be used?</legend>
          <div className="radio-cards">
            {TARGETS.map((option) => (
              <label
                key={option.value}
                aria-label={option.title}
                className={`radio-card${option.value === "runtime" ? "" : " radio-card--danger"}`}
              >
                <input
                  id={`secret-target-${option.value}`}
                  type="radio"
                  name="secret-target"
                  value={option.value}
                  checked={target === option.value}
                  onChange={() => setTarget(option.value)}
                />
                <span className="radio-card__body">
                  <strong>{option.title}</strong>
                  <span>{option.description}</span>
                </span>
              </label>
            ))}
          </div>
          {formErrors.target ? (
            <p className="field__error" role="alert">
              {formErrors.target}
            </p>
          ) : null}
        </fieldset>

        <fieldset className="fieldset">
          <legend>Environments</legend>
          <div className="checkbox-row">
            <Checkbox
              checked={environments.includes("production")}
              onChange={(checked) => toggleEnvironment("production", checked)}
              label="Production"
            />
            <Checkbox
              checked={environments.includes("preview")}
              onChange={(checked) => toggleEnvironment("preview", checked)}
              label="Preview"
              hint="Preview environments are often readable by pull-request code."
            />
          </div>
          {formErrors.environments ? (
            <p className="field__error" role="alert">
              {formErrors.environments}
            </p>
          ) : null}
        </fieldset>

        {needsConfirmation ? (
          <Callout tone="danger" title="This value leaves the runtime-only boundary">
            <p>
              Build-visible secrets can be read by repository code. Non-production environments may
              be reachable from pull requests or the public internet. Only continue if this exposure
              is intended.
            </p>
            <Checkbox
              checked={confirmation}
              onChange={setConfirmation}
              label="I understand that this secret may be visible to build code or a non-production environment."
              error={formErrors.confirmation}
            />
          </Callout>
        ) : null}

        <div className="form__actions">
          <Button type="submit" variant="primary" busy={put.isPending}>
            Save secret
          </Button>
        </div>
      </form>

      <ConfirmDialog
        open={deleteKey !== null}
        title={`Delete ${deleteKey ?? "secret"}?`}
        description={<p>This removes the key from the project’s secret metadata.</p>}
        consequence={`Deletes ${deleteKey ?? "this key"}${selectedDelete?.target === "build" || selectedDelete?.target === "both" ? ", which build code can currently read" : ""}. Deployments and running services that read it will fail until it is set again. This cannot be undone.`}
        confirmLabel="Delete secret"
        busy={remove.isPending}
        onCancel={() => setDeleteKey(null)}
        onConfirm={() => {
          if (deleteKey) remove.mutate(deleteKey);
        }}
      />

      {deleteError ? (
        <ErrorState
          error={deleteError}
          compact
          onReload={() => {
            setDeleteKey(null);
            setDeleteError(null);
            void secrets.refetch();
          }}
        />
      ) : null}
    </div>
  );
}

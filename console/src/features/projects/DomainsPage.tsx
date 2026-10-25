import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/endpoints";
import type { Domain } from "../../api/types";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { CopyButton } from "../../components/CopyButton";
import { EmptyState } from "../../components/EmptyState";
import { TextInput } from "../../components/Field";
import { LoadingPanel } from "../../components/Loading";
import { ErrorState } from "../../components/ProblemView";
import { StatusBadge } from "../../components/StatusBadge";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { formatDate } from "../../lib/format";
import { fieldErrors } from "../../lib/problem";

function DnsInstructions({ domain }: { domain: Domain }) {
  if (domain.dns_instructions.length === 0) {
    return <p className="text-muted">No DNS change is required.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="data-table data-table--compact">
        <caption className="visually-hidden">Exact DNS records for {domain.hostname}</caption>
        <thead>
          <tr>
            <th scope="col">Type</th>
            <th scope="col">Name</th>
            <th scope="col">Value</th>
            <th scope="col">TTL</th>
            <th scope="col">Copy</th>
          </tr>
        </thead>
        <tbody>
          {domain.dns_instructions.map((record, index) => (
            <tr key={`${record.type}-${record.name}-${index}`}>
              <td>
                <code>{record.type}</code>
              </td>
              <td>
                <code>{record.name}</code>
              </td>
              <td>
                <code>{record.value}</code>
              </td>
              <td>{record.ttl ?? "Default"}</td>
              <td>
                <CopyButton
                  value={`${record.type} ${record.name} ${record.value}`}
                  label="Copy record"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function DomainsPage() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const queryClient = useQueryClient();
  useDocumentTitle("Domains");

  const domains = useQuery({
    queryKey: ["domains", projectId],
    queryFn: ({ signal }) => api.domains.list(projectId, signal),
    enabled: Boolean(projectId),
  });

  const [hostname, setHostname] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [created, setCreated] = useState<Domain | null>(null);
  const [writeError, setWriteError] = useState<unknown>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<unknown>(null);

  const add = useMutation({
    mutationFn: () => api.domains.create(projectId, { hostname: hostname.trim() }),
    onSuccess: (domain) => {
      setCreated(domain);
      setHostname("");
      setErrors({});
      setWriteError(null);
      void queryClient.invalidateQueries({ queryKey: ["domains", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: (error) => {
      setWriteError(error);
      const apiErrors = fieldErrors(error);
      if (Object.keys(apiErrors).length > 0) setErrors(apiErrors);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.domains.remove(id),
    onSuccess: () => {
      setDeleteId(null);
      setDeleteError(null);
      void queryClient.invalidateQueries({ queryKey: ["domains", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: (error) => setDeleteError(error),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = hostname.trim().toLowerCase();
    const next: Record<string, string> = {};
    if (!/^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/.test(trimmed)) {
      next.hostname = "Use a hostname such as app.example.com.";
    }
    setErrors(next);
    setWriteError(null);
    if (Object.keys(next).length > 0) return;
    add.mutate();
  }

  const deleting = domains.data?.find((domain) => domain.id === deleteId) ?? null;

  return (
    <div className="stack stack--loose">
      {created ? (
        <section className="panel panel--dns" aria-labelledby="domain-created-title">
          <p className="eyebrow">Domain added</p>
          <h2 id="domain-created-title">Create this exact record for {created.hostname}</h2>
          <p>
            The domain stays <strong>pending verification</strong> until DNS resolves. Automatic
            verification is not wired into this local slice yet; the record above is what a DNS
            provider needs.
          </p>
          <DnsInstructions domain={created} />
          <Button variant="secondary" onClick={() => setCreated(null)}>
            Hide instructions
          </Button>
        </section>
      ) : null}

      <section className="panel" aria-labelledby="domains-list-title">
        <div className="panel__header">
          <div>
            <h2 id="domains-list-title">Custom domains</h2>
            <p className="panel__subtitle">
              Status, certificate expiry and the exact DNS record required for each hostname.
            </p>
          </div>
        </div>

        {domains.isPending ? <LoadingPanel label="Loading domains" lines={3} /> : null}
        {domains.isError ? (
          <ErrorState error={domains.error} onRetry={() => void domains.refetch()} />
        ) : null}
        {domains.data?.length === 0 ? (
          <EmptyState title="No custom domains">
            <p>Attach a hostname you control. The API returns the exact DNS record to create.</p>
          </EmptyState>
        ) : null}
        {domains.data && domains.data.length > 0 ? (
          <ul className="domain-list">
            {domains.data.map((domain) => (
              <li key={domain.id} className="domain-list__item">
                <div className="domain-list__heading">
                  <div>
                    <h3>{domain.hostname}</h3>
                    <div className="badge-row">
                      <StatusBadge status={domain.status} srPrefix="Domain status" />
                      {domain.is_production ? <StatusBadge status="production" /> : null}
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => {
                      setDeleteError(null);
                      setDeleteId(domain.id);
                    }}
                  >
                    Remove
                    <span className="visually-hidden"> {domain.hostname}</span>
                  </Button>
                </div>
                <p className="text-muted">
                  Certificate expires: {formatDate(domain.cert_expires_at)}
                </p>
                <DnsInstructions domain={domain} />
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <form className="panel form" onSubmit={submit} noValidate aria-labelledby="add-domain-title">
        <h2 id="add-domain-title">Add a domain</h2>
        {writeError && Object.keys(errors).length === 0 ? (
          <ErrorState error={writeError} compact />
        ) : null}
        <TextInput
          id="domain-hostname"
          label="Hostname"
          value={hostname}
          onChange={(event) => setHostname(event.target.value)}
          placeholder="app.example.com"
          hint="Lower-case DNS name only. The API returns the TXT record required for verification."
          error={errors.hostname}
          autoComplete="off"
          spellCheck={false}
          required
        />
        <div className="form__actions">
          <Button type="submit" variant="primary" busy={add.isPending}>
            Add domain
          </Button>
        </div>
      </form>

      <ConfirmDialog
        open={deleteId !== null}
        title={`Remove ${deleting?.hostname ?? "domain"}?`}
        description={
          <p>
            Removing a domain revokes its certificate (SPEC-36 §3.5). The hostname stops serving
            this project.
          </p>
        }
        consequence={`Removes ${deleting?.hostname ?? "this domain"} and revokes its certificate. Visitors to this hostname will see a connection failure. This cannot be undone.`}
        confirmLabel="Remove domain"
        busy={remove.isPending}
        onCancel={() => setDeleteId(null)}
        onConfirm={() => {
          if (deleteId) remove.mutate(deleteId);
        }}
      />

      {deleteError ? (
        <ErrorState
          error={deleteError}
          compact
          onReload={() => {
            setDeleteId(null);
            setDeleteError(null);
            void domains.refetch();
          }}
        />
      ) : null}
    </div>
  );
}

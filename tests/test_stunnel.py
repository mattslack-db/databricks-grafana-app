from lib.stunnel import render_conf


def test_render_conf_sets_sni_to_lakebase_host_in_client_mode():
    conf = render_conf(accept_port=5433,
                       server_host="ep-example-00000000.database.us-east-1.cloud.databricks.com",
                       server_port=5432)
    assert "client = yes" in conf
    assert "accept = 127.0.0.1:5433" in conf
    assert ("connect = ep-example-00000000.database.us-east-1.cloud.databricks.com:5432"
            in conf)
    # SNI must be the Lakebase hostname — this is the whole reason stunnel exists.
    assert ("sni = ep-example-00000000.database.us-east-1.cloud.databricks.com"
            in conf)
    assert "foreground = yes" in conf
    # Postgres STARTTLS negotiation is required (not TLS-on-connect).
    assert "protocol = pgsql" in conf


def test_render_conf_verify_chain_toggles_verification():
    off = render_conf(accept_port=5433, server_host="h", server_port=5432)
    assert "verifyChain = no" in off
    on = render_conf(accept_port=5433, server_host="h", server_port=5432,
                     verify_chain=True)
    assert "verifyChain = yes" in on
    assert "checkHost = h" in on

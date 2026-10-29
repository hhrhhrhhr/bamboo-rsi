import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;

/** JDK 11+, no third-party dependencies. Never hard-code the control token. */
public final class RsiClient {
    public static void main(String[] args) throws Exception {
        if (args.length != 3 && args.length != 5) {
            throw new IllegalArgumentException(
                "Usage: RsiClient <base-url> GET <path> | <base-url> POST <path> <json-file> <stable-idempotency-key>");
        }
        URI base = URI.create(args[0]);
        if ((!"https".equals(base.getScheme()) && !"http".equals(base.getScheme()))
                || base.getHost() == null || base.getUserInfo() != null
                || base.getQuery() != null || base.getFragment() != null
                || (!base.getPath().isEmpty() && !base.getPath().equals("/"))) {
            throw new IllegalArgumentException("Expected a credential-free HTTP(S) origin");
        }
        if (!args[2].matches("/v1/[a-z0-9/_.-]+")) {
            throw new IllegalArgumentException("Expected an RSI control API path");
        }
        String token = System.getenv("RSI_CONTROL_TOKEN");
        if (token == null || token.length() < 32) {
            throw new IllegalArgumentException("Set RSI_CONTROL_TOKEN in the process environment");
        }
        HttpRequest.Builder request = HttpRequest.newBuilder(base.resolve(args[2]))
            .timeout(Duration.ofSeconds(30))
            .header("Authorization", "Bearer " + token)
            .header("Accept", "application/json");
        if (args[1].equals("GET") && args.length == 3) {
            request.GET();
        } else if (args[1].equals("POST") && args.length == 5) {
            if (!args[4].matches("[A-Za-z0-9_.:-]{16,128}")) {
                throw new IllegalArgumentException("Keep the same idempotency key when recovering a lost response");
            }
            String body = Files.readString(Path.of(args[3]), StandardCharsets.UTF_8);
            request.header("Content-Type", "application/json")
                .header("Idempotency-Key", args[4])
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        } else {
            throw new IllegalArgumentException("Only GET or POST is supported");
        }
        HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10))
            .followRedirects(HttpClient.Redirect.NEVER).build();
        HttpResponse<String> response = client.send(request.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        System.out.println(response.body());
        if (response.statusCode() != 200 && response.statusCode() != 202) {
            System.exit(1);
        }
        // A 202 response is only acceptance of an operation. Poll its id via
        // GET /v1/operations/{id} and inspect status/result before claiming success.
    }
}

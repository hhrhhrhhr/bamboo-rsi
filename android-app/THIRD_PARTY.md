# Android dependencies

Original Bamboo RSI source is MIT. Dependencies retain their own terms; this repository does not relicense them.

- Google ML Kit Chinese OCR: com.google.mlkit:text-recognition-chinese:16.0.1. The model is bundled and processes images locally. The SDK sends performance/utilization metrics to Google. Review the SDK/model terms and disclose this processing before distribution.
- AndroidX and Google Play services are transitive dependencies. Retain vendor notices and review published Maven metadata for the resolved graph.
- JUnit 4.13.2 and org.json 20240303 are test-only, not packaged runtime dependencies.
- AndroidX Test runner 1.6.2, core 1.6.1 and ext:junit 1.2.1 are pinned device-test dependencies (Apache-2.0); they are packaged in the separate test APK, not the main application. They are not claimed to be the newest releases.
- Android Gradle Plugin 8.7.3 and Gradle 8.9 are build tools.

Generate the dependency report with:

    gradle -p android-app :app:dependencies --configuration debugRuntimeClasspath

This is an inventory, not completed legal approval or a comprehensive SBOM.

Tencent hok_env and GameAISDK are integration references, not bundled dependencies. See [TENCENT_INTEGRATION.md](TENCENT_INTEGRATION.md) for pinned references and their distinct licenses.

[ML Kit documentation](https://developers.google.com/ml-kit/vision/text-recognition/v2/android) · [Terms and privacy](https://developers.google.com/ml-kit/terms)

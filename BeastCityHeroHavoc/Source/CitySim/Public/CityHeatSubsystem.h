// City-wide wanted/heat escalation (0-5), per the design spec: rises from destruction/combat,
// decays only after sustained calm (no damage/combat and no line-of-sight for the decay windows).
#pragma once

#include "CoreMinimal.h"
#include "Subsystems/WorldSubsystem.h"
#include "Stats/Stats.h"
#include "CityHeatSubsystem.generated.h"

DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FOnHeatTierChanged, int32, NewTier);

UCLASS()
class CITYSIM_API UCityHeatSubsystem : public UTickableWorldSubsystem
{
	GENERATED_BODY()

public:
	static constexpr int32 MinHeatTier = 0;
	static constexpr int32 MaxHeatTier = 5;

	UPROPERTY(BlueprintAssignable, Category = "City|Heat")
	FOnHeatTierChanged OnHeatTierChanged;

	// Raises heat by Amount (clamped to MaxHeatTier) and resets the decay timer.
	UFUNCTION(BlueprintCallable, Category = "City|Heat")
	void AddHeat(int32 Amount);

	// Immediately zeroes heat (e.g. debug / mission reset).
	UFUNCTION(BlueprintCallable, Category = "City|Heat")
	void ResetHeat();

	UFUNCTION(BlueprintPure, Category = "City|Heat")
	int32 GetHeatTier() const { return CurrentHeatTier; }

	// Seconds of no aggression required before heat starts decaying one tier at a time.
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "City|Heat")
	float DecayDelaySeconds = 8.0f;

	// Seconds between successive tier decrements once decaying.
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "City|Heat")
	float DecayTickSeconds = 4.0f;

	// UTickableWorldSubsystem
	virtual void Tick(float DeltaTime) override;
	virtual TStatId GetStatId() const override;
	virtual bool DoesSupportWorldType(EWorldType::Type WorldType) const override;

private:
	UPROPERTY()
	int32 CurrentHeatTier = 0;

	double LastAggressionTimeSeconds = -1000.0;
	double LastDecayTimeSeconds = -1000.0;

	void SetHeatTier(int32 NewTier);
};
